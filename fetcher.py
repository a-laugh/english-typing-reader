#!/usr/bin/env python3
"""构建脚本：抓取最近 N 天 BBC 6 Minute English，烘焙静态站数据。

BBC 每周四更新一期，默认滚动窗口 30 天（在架约 4-5 期），
由 GitHub Actions 每周日凌晨跑一次（也支持手动触发）。
产物：剧集 JSON（含逐词词典，提交进仓库）+ 单词发音 MP3
（edge-tts，不进仓库——由 workflow 缓存并随 Pages 部署包分发）。
本地运行需能访问 BBC，如走代理用标准环境变量：HTTPS_PROXY=... 
    uv run python fetcher.py [--days 30] [--no-llm] [--no-audio]

产物写入 docs/data/episodes/：
    {id}.json     单期完整数据（transcript、词汇表、语境词表、逐词词典）
    index.json    当前窗口内剧集列表（按发布日期倒序）
超过 N 天的剧集文件会被清理（滚动窗口）。
"""

import argparse
import ast
import asyncio
import json
import os
import re
import sys
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

import edge_tts
import requests
from bs4 import BeautifulSoup

INDEX_URL = "https://www.bbc.co.uk/learningenglish/english/features/6-minute-english"
EPISODE_PATH_RE = re.compile(
    r"^/learningenglish/english/features/6-minute-english_(\d{4})/ep-(\d{6})$"
)
DATA_DIR = Path(__file__).resolve().parent / "docs" / "data" / "episodes"

# 单词发音 MP3 缓存（构建期烘焙，跨剧集共享；前端优先播放，浏览器 TTS 兜底）
AUDIO_DIR = DATA_DIR.parent / "audio"
TTS_VOICE = os.environ.get("TTS_VOICE") or "en-US-AriaNeural"
TTS_CONCURRENCY = 8
TTS_TIMEOUT = 15.0

REQUEST_TIMEOUT = 30
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "en-GB,en;q=0.9",
}

# GLM（智谱）OpenAI 兼容接口，key 通过环境变量注入，模型可用环境变量覆盖。
# 用 or 兜底：CI 里 ${{ vars.X }} 未配置时会注入空串，空串不应覆盖默认值。
# 默认 glm-4-air（按量计费，频控宽松）：glm-4-flash 是免费模型，无论账户余额
# 都受免费档严格频控，分块连续调用实测持续 429
LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4"
LLM_MODEL = os.environ.get("LLM_MODEL") or "glm-4-air"

# 词汇表短语做逐词索引时跳过的虚词（避免 "the grid" 把 the 也建词条）
STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "is", "are",
    "be", "been", "was", "were", "it", "its", "this", "that", "these", "those",
}

# 站点端单词清洗规则（与 docs/script.js 的 cleanWord 保持一致）
WORD_RE = re.compile(r"[a-z0-9]+")


def log(msg: str) -> None:
    print(f"[fetcher] {msg}", flush=True)


def http_get(url: str) -> str:
    resp = requests.get(url, headers=HTTP_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------------
# 索引页解析
# ---------------------------------------------------------------------------

def parse_index(html: str) -> list[dict]:
    """解析栏目索引页，返回去重后的剧集列表（按页面出现顺序）。

    ep 链接形如 /learningenglish/english/features/6-minute-english_2026/ep-261029，
    ep 号本身编码日期 YYMMDD，与 URL 年份段互相印证。
    """
    soup = BeautifulSoup(html, "lxml")
    seen: dict[str, dict] = {}
    for a in soup.select("h2 a[href]"):
        m = EPISODE_PATH_RE.match(a.get("href", ""))
        if not m:
            continue
        year, ep_num = int(m.group(1)), m.group(2)
        try:
            pub_date = date(year, int(ep_num[2:4]), int(ep_num[4:6]))
        except ValueError:
            continue
        ep_id = ep_num
        if ep_id not in seen:
            seen[ep_id] = {
                "id": ep_id,
                "ep_num": ep_num,
                "url": f"https://www.bbc.co.uk{a['href']}",
                "title": a.get_text(strip=True),
                "pub_date": pub_date.isoformat(),
            }
    return list(seen.values())


# ---------------------------------------------------------------------------
# 剧集页解析
# ---------------------------------------------------------------------------

def _iter_turns(p, turns: list[dict]) -> None:
    """把一个 transcript <p> 中的对话回合追加到 turns（跨 <p> 续接）。

    结构规则（2026 版页面）：
      <strong>说话人</strong><br/>台词…   —— strong 后紧跟 <br/> 的是说话人，开新回合
      <strong>词</strong>台词…            —— 不跟 <br/> 的是句中强调，保留词本身
      <br/> 与空白/&nbsp; 混合             —— 回合内换行，折叠为空格（回合边界只由说话人 strong 决定）
    """
    current = turns[-1] if turns else None
    for child in p.children:
        name = getattr(child, "name", None)
        if name == "strong":
            nxt = child.next_sibling
            if nxt is not None and getattr(nxt, "name", None) == "br":
                # 说话人 strong：开新回合（同说话人连续发言也各自成段）
                current = {"speaker": child.get_text(strip=True), "text": ""}
                turns.append(current)
                continue
            if current is not None:
                current["text"] += child.get_text()
        elif name == "br":
            # 回合内换行（如测验选项）：补一个空格，避免前后文本粘连
            if current is not None:
                current["text"] += " "
        else:
            text = child.get_text() if name else str(child)
            text = text.replace("\xa0", " ")
            if current is not None:
                current["text"] += text
    # 行尾与重复空白由调用方统一规整


def _looks_like_turn_start(p) -> bool:
    """判断 <p> 是否以「说话人 strong + <br/>」开头（用于识别页脚并停止）。"""
    first = p.find("strong", recursive=False) or next(
        (c for c in p.children if getattr(c, "name", None) == "strong"), None
    )
    if first is None or first.find_parent("p") is not p:
        return False
    # strong 必须是 p 的首个非空内容子节点
    for c in p.children:
        if getattr(c, "name", None) == "strong":
            break
        if getattr(c, "name", None) is None and str(c).strip():
            return False
    nxt = first.next_sibling
    return nxt is not None and getattr(nxt, "name", None) == "br"


def parse_episode(html: str, meta: dict) -> dict:
    """解析单期剧集页，提取标题 / transcript / 官方词汇表 / mp3 链接。"""
    soup = BeautifulSoup(html, "lxml")

    title = meta["title"]
    og = soup.select_one("meta[property='og:title']")
    if og and og.get("content", "").strip():
        # 形如 "BBC Learning English - 6 Minute English / 真实标题"
        title = og["content"].split(" / ")[-1].strip() or title

    turns_raw: list[dict] = []
    anchor = next(
        (p for p in soup.find_all("p") if p.get_text(strip=True).upper() == "TRANSCRIPT"),
        None,
    )
    if anchor is None:
        raise ValueError("页面中未找到 TRANSCRIPT 锚点")
    started = False
    for p in anchor.parent.find_all("p"):
        if p is anchor:
            started = True
            continue
        if not started:
            continue
        text = p.get_text(" ", strip=True)
        if not text:
            continue
        if text.lower().startswith("note:"):
            # "Note: This is not a word-for-word transcript." 官方说明行
            continue
        if turns_raw and not _looks_like_turn_start(p):
            # 对白之后不以说话人开头的段落是页脚（"What do you think…" 等）
            break
        if not turns_raw and not _looks_like_turn_start(p):
            # 对白尚未开始时出现的不明段落，防御性跳过
            log(f"跳过 TRANSCRIPT 锚点后的非对白段落: {text[:60]!r}")
            continue
        _iter_turns(p, turns_raw)
    # 规整：回合内空白折叠、丢弃无名/空文本回合；transcript 保持「回合文本按行拼接」
    turns = [
        {"speaker": t["speaker"], "text": " ".join(t["text"].split())}
        for t in turns_raw
        if t["speaker"] and t["text"].strip()
    ]
    transcript = "\n".join(t["text"] for t in turns)
    if len(transcript.split()) < 50:
        raise ValueError(f"transcript 过短（{len(transcript.split())} 词），疑似解析失败")

    vocabulary: list[dict] = []
    for h3 in soup.find_all("h3"):
        if h3.get_text(strip=True) != "Vocabulary":
            continue
        vp = h3.find_next_sibling("p")
        if vp is None:
            break
        for strong in vp.find_all("strong"):
            word = strong.get_text(strip=True)
            if not word:
                continue
            # 释义 = 从当前 strong 之后到下一个 strong 之前的文本
            chunks: list[str] = []
            for sib in strong.next_siblings:
                if getattr(sib, "name", None) == "strong":
                    break
                chunks.append(sib.get_text() if sib.name else str(sib))
            definition = " ".join(" ".join(chunks).split()).strip(" |")
            vocabulary.append({"word": word, "definition": definition})
        break

    mp3_url = ""
    a_mp3 = soup.select_one("a.bbcle-download-extension-mp3")
    if a_mp3 and a_mp3.get("href"):
        mp3_url = a_mp3["href"]

    return {
        "id": meta["id"],
        "title": title,
        "pub_date": meta["pub_date"],
        "bbc_url": meta["url"],
        "mp3_url": mp3_url,
        "transcript": transcript,
        "turns": turns,
        "vocabulary": vocabulary,
    }


# ---------------------------------------------------------------------------
# GLM 语境词表
# ---------------------------------------------------------------------------

GLOSSARY_PROMPT = """你是英语学习材料的编辑。下面是一期 BBC 6 Minute English 的英文对白，\
以及该节目官方给出的重点词汇表。

请找出本段对白中**每一个值得学习的英文单词与短语**并给出中文释义——宁多勿漏。
不需要收录的仅限最基础的虚词与口语填充词（如 the、a、and、but、of、to、in、on、
is、are、was、were、be、have、has、do、does、it、this、that、there、here、
I、you、he、she、we、they、my、your、his、her、our、their、me、him、them、
not、no、so、very、just、really、well、oh、yeah、OK、yes、nope、um、er）。
除此之外的动词、名词、形容词、副词、习语短语**全部**都要给出释义。

释义要求：不超过 10 个字，简洁、易懂（例如"电网"、"高温"、"居住"）；
不要解释性长句、不要堆砌词性、不要加括号补充说明。
- 官方词汇表中的词：以官方英文释义为依据，转成简洁中文
- 其余词：结合对白语境给出中文含义

仅输出一个 JSON 对象（最外层是一对花括号，键为单词、值为对象，全部用双引号），
不要输出任何其他文字。示例格式：
{
  "单词": {"meaning": "不超过10字的中文释义", "sentence": "该词所在的对白句子（英文原文，保留该词）"},
  "the grid": {"meaning": "电网", "sentence": "..."}
}
"""

GLOSSARY_PROMPT_NO_VOCAB = """你是英语学习材料的编辑。下面是一期 BBC 6 Minute English 的英文对白片段。
请找出本段对白中**每一个值得学习的英文单词与短语**并给出中文释义——宁多勿漏。
不需要收录的仅限最基础的虚词与口语填充词（如 the、a、and、but、of、to、in、on、
is、are、was、were、be、have、has、do、does、it、this、that、there、here、
I、you、he、she、we、they、my、your、his、her、our、their、me、him、them、
not、no、so、very、just、really、well、oh、yeah、OK、yes、nope、um、er）。
除此之外的动词、名词、形容词、副词、习语短语**全部**都要给出释义。
释义不超过 10 个字，简洁、易懂（例如"电网"、"高温"、"居住"）。

仅输出一个 JSON 对象（最外层是一对花括号，键为单词、值为对象，全部用双引号），
不要输出任何其他文字。示例格式：
{
  "单词": {"meaning": "不超过10字的中文释义", "sentence": "该词所在的对白句子（英文原文，保留该词）"}
}
"""

# 单块对白目标词数：整期约 1000+ 词，一次喂给 LLM 会导致输出截断/覆盖锐减
# （实测整篇提示只产出 6-20 条），按回合边界分块、逐块生成后合并。
# 400 词/块兼顾输出上限（~60 词条 < max_tokens 4500）与调用次数（免费档限流敏感）
GLOSSARY_CHUNK_WORDS = 400
GLOSSARY_CHUNK_PAUSE = 2.0  # 块间停顿（秒），降低免费档 429 限流概率


def _chunk_transcript(transcript: str, chunk_words: int) -> list[list[str]]:
    """按行（回合）把对白切分成 ~chunk_words 的块，不切断回合。"""
    lines = [ln for ln in transcript.split("\n") if ln.strip()]
    chunks: list[list[str]] = []
    current: list[str] = []
    count = 0
    for line in lines:
        words = len(line.split())
        if current and count + words > chunk_words:
            chunks.append(current)
            current, count = [], 0
        current.append(line)
        count += words
    if current:
        chunks.append(current)
    return chunks


def _glm_glossary_call(prompt: str, api_key: str, label: str) -> dict:
    """单次 GLM 调用并解析为词表 dict；失败返回 {}。

    429 限流（免费档 rpm 低，分块连续调用实测会触发）单独处理：
    指数退避最多等 5 轮并尊重 Retry-After；解析失败重试 1 次。
    """
    url = f"{LLM_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "user", "content": prompt},
            {
                "role": "system",
                "content": "You are a precise assistant that only outputs valid JSON.",
            },
        ],
        "temperature": 0.2,
        "max_tokens": 4500,
    }
    rate_limit_hits = 0
    attempt = 0
    content = None
    while attempt < 3:
        attempt += 1
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            if resp.status_code == 429:
                # 带上响应体：bigmodel 的 429 body 含具体错误码
                # （如 1302 并发/频控、免费额度耗尽等），Actions 日志可直接定位
                log(f"{label} 429 响应体: {resp.text[:200]!r}")
            if resp.status_code == 429 and rate_limit_hits < 5:
                rate_limit_hits += 1
                wait = min(2 ** rate_limit_hits * 2, 30)  # 4s, 8s, 16s, 30s, 30s
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.strip().isdigit():
                    wait = min(int(retry_after), 60)
                log(f"{label} 429 限流，等待 {wait}s 后重试（第 {rate_limit_hits} 次）")
                time.sleep(wait)
                attempt -= 1  # 限流等待不计入解析重试次数
                continue
            resp.raise_for_status()
            try:
                content = resp.json()["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError) as e:
                # HTTP 200 但 body 不是预期结构（网关故障页等）：
                # 把响应体片段带进异常消息，方便从 Actions 日志定位
                raise ValueError(f"GLM 响应结构异常（{e}），body 前 200 字符: {resp.text[:200]!r}")
            glossary = _parse_json_block(content)
            if not isinstance(glossary, dict):
                raise ValueError("LLM 输出不是 JSON 对象")
            return glossary
        except Exception as e:
            if attempt >= 3:
                log(f"{label} 第 {attempt} 次尝试失败（放弃）: {e}")
                break
            log(f"{label} 第 {attempt} 次尝试失败，1s 后重试: {e}")
            if isinstance(e, ValueError) and "GLM 响应结构异常" not in str(e) and content:
                # 解析失败时留一小段原始输出，方便从 Actions 日志定位模型行为
                log(f"GLM 原始输出前 200 字符: {str(content)[:200]!r}")
            time.sleep(1)
    return {}


def generate_context_glossary(transcript: str, vocabulary: list, api_key: str) -> dict:
    """分块调用 GLM 生成全篇语境词表（官方词汇表优先作为释义依据）。

    单块失败跳过（下一块继续），全部失败返回 {}。
    """
    chunks = _chunk_transcript(transcript, GLOSSARY_CHUNK_WORDS)
    vocab_lines = "\n".join(
        f"{item.get('word', '')}: {item.get('definition', '')}".strip()
        for item in vocabulary if item.get("word")
    )
    glossary: dict = {}
    empty_streak = 0  # 连续空块计数：疑似配额耗尽时提前止损，把机会留给下次构建
    for ci, chunk in enumerate(chunks, 1):
        if ci > 1:
            time.sleep(GLOSSARY_CHUNK_PAUSE)  # 块间节奏，降低免费档限流概率
        known = "、".join(sorted(glossary.keys()))
        parts = [GLOSSARY_PROMPT if vocab_lines else GLOSSARY_PROMPT_NO_VOCAB]
        if vocab_lines:
            parts.append(f"官方词汇表（全期通用）：\n{vocab_lines}")
        if known:
            parts.append(f"以下词已给出过释义，不要再输出：{known}")
        parts.append("本段对白：\n" + "\n".join(chunk))
        part = _glm_glossary_call("\n\n".join(parts), api_key, f"语境词表块 {ci}/{len(chunks)}")
        for word, info in part.items():
            glossary.setdefault(word, info)
        log(f"语境词表块 {ci}/{len(chunks)}：本块 {len(part)} 词条，累计 {len(glossary)} 词条")
        empty_streak = empty_streak + 1 if not part else 0
        if empty_streak >= 2:
            log(f"警告：连续 {empty_streak} 块生成失败（大概率配额/限流未恢复），"
                f"本期剩余块跳过，等待下次构建补齐")
            break
    return glossary


def _parse_json_block(text: str):
    """容错提取 LLM 输出中的 JSON。

    依次尝试：剥掉 ```json 围栏后的整段 → 截取首尾大括号之间；
    每个候选先 json.loads，失败再 ast.literal_eval——
    模型偶尔无视提示输出单引号的 Python 风格字典（实测 glm-4-flash 出现过），
    literal_eval 恰好能解析该格式（纯字面量、无代码执行，安全），也容忍尾逗号。
    """
    def parse(candidate: str):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        try:
            return ast.literal_eval(candidate)
        except (ValueError, SyntaxError, TypeError):
            # TypeError：如「以 dict 为 key」的字面量（unhashable type: 'dict'，实测出现过）
            pass
        raise ValueError("无法解析为 JSON/Python 字面量")

    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        sliced = text[start : end + 1]
        candidates.append(sliced)
        # 防御：模型偶尔照抄示例里的双层花括号 {{...}}（历史 bug），
        # 外层就被解析成「以 dict 为 key」而报 unhashable type: 'dict'
        if sliced.startswith("{{") and sliced.endswith("}}"):
            candidates.append(sliced[1:-1])
    last_error = None
    for candidate in candidates:
        try:
            return parse(candidate)
        except ValueError as e:
            last_error = e
    raise ValueError(f"输出中找不到可解析的 JSON 对象（{last_error}）")


# ---------------------------------------------------------------------------
# 构建期 TTS 音频（edge-tts）
# ---------------------------------------------------------------------------

def collect_words(episode: dict) -> dict[str, str]:
    """收集一期剧集需要发音的单词，返回 {清洗后词: 代表原文}。

    键与前端 cleanWord/音频文件名一致（仅小写字母数字）；值保留一个原文写法
    （如 we've / six-minute）交给 TTS，避免合成器读连写词。
    词汇表/语境词表中的短语（the grid）与词典层别名规则一致：额外收录
    非虚词成分（grid）——前端按空白分词，永远只会查到成分词。
    """
    words: dict[str, str] = {}

    def remember(token: str):
        cleaned = clean_word(token)
        if cleaned and cleaned not in words:
            words[cleaned] = token

    def remember_with_parts(token: str):
        remember(token)
        if " " in token:
            for part in WORD_RE.findall(token.lower()):
                if part not in STOPWORDS:
                    remember(part)

    for token in episode["transcript"].split():
        remember(token)
    for item in episode.get("vocabulary", []):
        remember_with_parts(item.get("word", ""))
    for key in episode.get("context_glossary", {}):
        remember_with_parts(key)
    return words


async def _tts_one(cleaned: str, tts_text: str) -> None:
    """生成单个单词 MP3：临时文件 + 原子替换，任一失败不留半截缓存。"""
    tmp_path = AUDIO_DIR / f".{cleaned}.{os.getpid()}.{uuid.uuid4().hex}.part"
    try:
        for attempt in (1, 2):
            try:
                communicate = edge_tts.Communicate(tts_text, TTS_VOICE)
                await asyncio.wait_for(communicate.save(str(tmp_path)), TTS_TIMEOUT)
                if tmp_path.exists() and tmp_path.stat().st_size > 0:
                    os.replace(tmp_path, AUDIO_DIR / f"{cleaned}.mp3")
                    return
                raise RuntimeError("生成内容为空")
            except Exception as e:
                tmp_path.unlink(missing_ok=True)
                if attempt == 2:
                    raise
                log(f"TTS 重试 {cleaned}（{e}）")
                await asyncio.sleep(1)
    finally:
        tmp_path.unlink(missing_ok=True)


async def _generate_missing(words: dict[str, str]) -> tuple[int, int]:
    sem = asyncio.Semaphore(TTS_CONCURRENCY)
    generated = failed = 0

    async def worker(cleaned: str, tts_text: str):
        nonlocal generated, failed
        async with sem:
            try:
                await _tts_one(cleaned, tts_text)
                generated += 1
            except Exception as e:
                failed += 1
                log(f"TTS 失败 {cleaned}: {e}")

    await asyncio.gather(*(worker(c, t) for c, t in words.items()))
    return generated, failed


def generate_missing_audio(episodes: list[dict]) -> tuple[int, int]:
    """为在架剧集的全部单词补齐缺失音频（跳过已有，跨剧集共享去重）。

    返回 (generated, failed)。TTS 服务不可达只警告不失败——前端会兜底浏览器 TTS。
    """
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    # 清理上次运行遗留的临时片段
    for stale in AUDIO_DIR.glob("*.part"):
        stale.unlink(missing_ok=True)

    merged: dict[str, str] = {}
    for episode in episodes:
        for cleaned, original in collect_words(episode).items():
            merged.setdefault(cleaned, original)
    missing = {
        c: t for c, t in merged.items() if not (AUDIO_DIR / f"{c}.mp3").exists()
    }
    if not missing:
        return 0, 0
    log(f"TTS 音频：共 {len(merged)} 词，缺 {len(missing)}，开始生成（{TTS_VOICE}）…")
    generated, failed = asyncio.run(_generate_missing(missing))
    log(f"TTS 音频：新生成 {generated} 个，失败 {failed} 个"
        + ("（前端将回退浏览器 TTS）" if failed else ""))
    return generated, failed


def prune_audio(referenced: set[str]) -> int:
    """删除不再被任何在架剧集引用的音频与遗留临时文件，保持 docs/data 有界。"""
    if not AUDIO_DIR.exists():
        return 0
    removed = 0
    for f in AUDIO_DIR.iterdir():
        if f.suffix == ".part" or (f.suffix == ".mp3" and f.stem not in referenced):
            f.unlink()
            removed += 1
    if removed:
        log(f"已清理 {removed} 个无引用/残留音频")
    return removed


# ---------------------------------------------------------------------------
# 烘焙逐词词典
# ---------------------------------------------------------------------------

def clean_word(token: str) -> str:
    """与前端 cleanWord 一致：小写、仅保留字母数字。"""
    subs = WORD_RE.findall(token.lower())
    return "".join(subs)


def bake_word_dict(episode: dict) -> dict:
    """合并释义来源，产出全篇逐词词典 {word: {translation, context?, sentence?, source}}。

    优先级：GLM 语境词表（官方词汇表的词也由 LLM 依据官方释义转成了简洁中文）>
    官方词汇表原始释义（LLM 缺失时的回退）。没有词典层——未覆盖的词前端显示「未收录」。
    短语（如 the grid）会额外为非虚词成分建索引。
    """
    baked: dict = {}

    def put(key: str, source: str, translation: str = "", context: str = "", sentence: str = ""):
        key = key.strip().lower()
        if not key or key in baked:
            return
        entry = {"translation": translation or context, "source": source}
        if context:
            entry["context"] = context
        if sentence:
            entry["sentence"] = sentence
        baked[key] = entry

    for word, info in episode.get("context_glossary", {}).items():
        if not isinstance(info, dict):
            continue
        put(
            word,
            "context",
            translation=str(info.get("meaning", "")),
            context=str(info.get("meaning", "")),
            sentence=str(info.get("sentence", "")),
        )

    for item in episode.get("vocabulary", []):
        put(item["word"], "official", translation=item.get("definition", ""))

    # 短语成分索引（the grid -> grid）与连字符索引（life-saving -> lifesaving），
    # 只补尚未覆盖的词；前端按空白分词，永远拿不到含空格的短语键，必须靠别名。
    # 别名按排序遍历：set 迭代顺序受字符串哈希随机化影响，不排序会导致
    # 同一剧集两次构建字节不同，破坏「产物确定性 → Actions 无空提交」的前提
    for phrase in list(baked):
        entry = baked[phrase]
        aliases = set()
        if " " in phrase:
            aliases.update(w for w in WORD_RE.findall(phrase) if w not in STOPWORDS)
        aliases.add(clean_word(phrase))
        for alias in sorted(aliases):
            if alias and alias not in baked:
                baked[alias] = dict(entry, phrase=phrase)

    return baked


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def build(days: int, no_llm: bool, no_audio: bool = False, rebake: bool = False) -> int:
    today = date.today()
    cutoff = today - timedelta(days=days - 1)  # 含今天在内的 N 天窗口
    api_key = os.environ.get("GLM_API_KEY", "")
    log(f"窗口：{cutoff} ~ {today}（{days} 天）；LLM：{'关' if no_llm or not api_key else '开'}；"
        f"音频：{'关' if no_audio else '开'}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if rebake:
        # 词典源修复/变更后的数据修复：为在架剧集重建逐词词典
        # （保留 transcript 与已生成的 context_glossary，不重新调 GLM）。
        # 独立模式：不抓 BBC、不清理、不生成音频，完成即退出
        rebake_count = 0
        for f in DATA_DIR.glob("*.json"):
            if f.name == "index.json":
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                data["dict"] = bake_word_dict(data)
                f.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                rebake_count += 1
            except (json.JSONDecodeError, OSError, KeyError) as e:
                log(f"警告：rebake 跳过 {f.name}（{e}）")
        log(f"rebake 完成：重建 {rebake_count} 期词典")
        return 0

    episodes = parse_index(http_get(INDEX_URL))
    if not episodes:
        # HTTP 200 但一个剧集链接都没解析出来 = BBC 改版（或返回了错误页）。
        # 此时绝不进入清理逻辑：保持货架与 index.json 原样，退出码 1 让 Actions 显示红
        log("错误：索引页解析出 0 期剧集（BBC 页面改版？），保留现有数据并退出")
        return 1
    recent = [ep for ep in episodes if date.fromisoformat(ep["pub_date"]) >= cutoff]
    log(f"索引页共 {len(episodes)} 期，窗口内 {len(recent)} 期：{[e['id'] for e in recent]}")

    failures = 0
    rebaked = 0  # 一次性补烘语境词表的已有剧集数
    skipped = 0  # 已在架、无需任何处理的剧集数
    for meta in recent:
        out_file = DATA_DIR / f"{meta['id']}.json"
        if out_file.exists():
            # 已存在的剧集原则上跳过（幂等）；但若此前构建时 GLM 不可用/失败，
            # context_glossary 为空且本次有 key，则补烘语境词表并重建词典，
            # 避免「一次降级被永久固化」
            if not no_llm and api_key:
                try:
                    data = json.loads(out_file.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        data = None
                except (json.JSONDecodeError, OSError):
                    data = None
                # 与主路径同等的防御：半损坏文件只跳过补烘，不让异常炸掉整个构建
                if data and "transcript" in data and not data.get("context_glossary"):
                    log(f"补烘语境词表：{meta['id']}（此前构建无 GLM 结果）")
                    glossary = generate_context_glossary(
                        data["transcript"], data.get("vocabulary", []), api_key
                    )
                    if glossary:
                        data["context_glossary"] = glossary
                        data["dict"] = bake_word_dict(data)
                        out_file.write_text(
                            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        rebaked += 1
                        log(f"已更新 {out_file.name}，词典 {len(data['dict'])} 条")
                    continue
                if data:
                    log(f"跳过已存在剧集：{meta['id']}"
                        f"（语境词表 {len(data.get('context_glossary') or {})} 条，非空不再重新生成）")
                    skipped += 1
                    continue
            log(f"跳过已存在剧集：{meta['id']}")
            skipped += 1
            continue
        try:
            episode = parse_episode(http_get(meta["url"]), meta)
            if not no_llm and api_key:
                episode["context_glossary"] = generate_context_glossary(
                    episode["transcript"], episode.get("vocabulary", []), api_key
                )
            else:
                episode["context_glossary"] = {}
            episode["dict"] = bake_word_dict(episode)
            out_file.write_text(
                json.dumps(episode, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            log(f"已写入 {out_file.name}：{len(episode['transcript'].split())} 词对白，"
                f"词典 {len(episode['dict'])} 条")
        except Exception as e:
            failures += 1
            log(f"剧集 {meta['id']} 处理失败: {e}")
    log(f"本次构建：新增 {len(recent) - skipped - rebaked - failures} 期，"
        f"补烘 {rebaked} 期，跳过（已在架）{skipped} 期")

    # 滚动窗口：清理过期剧集，再重建 index.json
    kept = prune_and_reindex(DATA_DIR, cutoff)

    # 构建期 TTS：为在架剧集补齐缺失音频，并清掉无引用的过期音频
    referenced_words: set[str] = set()
    for ep in kept:
        referenced_words |= set(collect_words(ep))
    if not no_audio:
        generate_missing_audio(kept)
    prune_audio(referenced_words)

    # 全部失败且货架为空 → 视为构建失败，让 Actions 显示红并保留重试机会；
    # 货架上还有旧剧集时降级为警告（站点仍可用，下次构建会重试缺失剧集）
    if failures and not kept:
        return 1
    return 0


def prune_and_reindex(data_dir: Path, cutoff: date) -> list[dict]:
    """删除 pub_date 早于 cutoff 的剧集与损坏文件，重建确定性排序的 index.json。

    返回保留的剧集列表。产物保持确定性：无新剧集/无过期剧集时不产生 git diff，
    Actions 的「有 diff 才提交」因此不会产生空提交。
    """
    kept: list[dict] = []
    removed = 0
    for f in data_dir.glob("*.json"):
        if f.name == "index.json":
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            pub = date.fromisoformat(data["pub_date"])
        except (json.JSONDecodeError, OSError, ValueError, KeyError) as e:
            log(f"警告：无法解析 {f.name}（{e}），删除该损坏文件")
            f.unlink()
            removed += 1
            continue
        if pub < cutoff:
            f.unlink()
            removed += 1
        else:
            kept.append(data)
    if removed:
        log(f"已清理 {removed} 个过期/损坏文件")

    kept.sort(key=lambda d: (d["pub_date"], d["id"]), reverse=True)
    index_entries = [
        {k: d[k] for k in ("id", "title", "pub_date", "bbc_url")} for d in kept
    ]
    (data_dir / "index.json").write_text(
        json.dumps(index_entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log(f"index.json：{len(index_entries)} 期在架")
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="滚动窗口天数（默认 30）")
    parser.add_argument("--no-llm", action="store_true", help="跳过 GLM 语境词表生成")
    parser.add_argument("--no-audio", action="store_true", help="跳过 TTS 发音音频生成")
    parser.add_argument("--rebake", action="store_true",
                        help="只为在架剧集重建逐词词典（保留语境词表，不调 GLM）")
    args = parser.parse_args()
    try:
        return build(args.days, args.no_llm, args.no_audio, args.rebake)
    except requests.RequestException as e:
        log(f"网络请求失败（索引页不可达？）: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
