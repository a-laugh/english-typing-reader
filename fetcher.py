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

# 全局内建词表（历史剧集挖掘 + 周构建自生长）：{清洗词: 中文释义}。
# 前端释义查询链：剧集词典（LLM 语境）→ 本表 → basics.json
GLOSSARY_FILE = DATA_DIR.parent / "glossary.json"

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
# 默认 glm-5.3-flash（用户购买的资源包模型）；glm-4-flash 免费档频控过严，
# 分块连续调用实测持续 429
LLM_BASE_URL = os.environ.get("LLM_BASE_URL") or "https://open.bigmodel.cn/api/paas/v4"
LLM_MODEL = os.environ.get("LLM_MODEL") or "glm-5.3-flash"

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

请给出本段对白中**每一个单词与短语**的中文释义——包括 the、and、I 这类最基础的词
（学习者要求所有词都有解释）。基础词给最简释义即可（如 the→定冠词、and→和、
I→我）。

释义要求：不超过 10 个字，简洁、易懂（例如"电网"、"高温"、"居住"）；
不要解释性长句、不要堆砌词性、不要加括号补充说明。
- 官方词汇表中的词：以官方英文释义为依据，转成简洁中文
- 其余词：结合对白语境给出中文含义

仅输出一个 JSON 对象（最外层是一对花括号，键为单词、值为只含 meaning 的对象，
全部用双引号）。不要输出任何其他文字。示例格式：
{
  "单词": {"meaning": "不超过10字的中文释义"},
  "the grid": {"meaning": "电网"}
}
"""

GLOSSARY_PROMPT_NO_VOCAB = """你是英语学习材料的编辑。下面是一期 BBC 6 Minute English 的英文对白片段。
请给出本段对白中**每一个单词与短语**的中文释义——包括 the、and、I 这类最基础的词
（学习者要求所有词都有解释）。基础词给最简释义即可（如 the→定冠词、and→和、
I→我）。释义不超过 10 个字，简洁、易懂。

仅输出一个 JSON 对象（最外层是一对花括号，键为单词、值为只含 meaning 的对象，
全部用双引号）。不要输出任何其他文字。示例格式：
{
  "单词": {"meaning": "不超过10字的中文释义"}
}
"""

# 单块对白目标词数：整期约 1000+ 词，一次喂给 LLM 会导致输出截断/覆盖锐减
# （实测整篇提示只产出 6-20 条），按回合边界分块、逐块生成后合并。
# 全词覆盖后词条数约翻三倍，块缩回 300 词（~120 词条 < max_tokens 4500）
GLOSSARY_CHUNK_WORDS = 300
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
    # 思考参数按官方文档分档（docs.bigmodel.cn 对话补全 API）：
    # - glm-5.3 / glm-5.3-flash 系列：恒定思考，thinking 只能 enabled，
    #   强度由独立字符串参数 reasoning_effort 控制（仅 low/high/max）
    # - 其余 glm-4.5+/5.x：支持 thinking.type=disabled，词表生成无需思考
    # 若端点拒绝（400），自适应摘除这些参数重试（见下方 400 分支）
    if re.match(r"glm-(5\.3|4\.7)", LLM_MODEL):
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = "low"
    elif re.match(r"glm-(4\.[5-9]|[5-9])", LLM_MODEL):
        payload["thinking"] = {"type": "disabled"}
    rate_limit_hits = 0
    attempt = 0
    content = None
    thinking_removed = False
    while attempt < 3:
        attempt += 1
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            if resp.status_code == 400 and not thinking_removed and (
                "thinking" in payload or "reasoning_effort" in payload
            ):
                # 端点不接受思考相关参数（各模型/版本支持不一）：
                # 打印响应体后自动摘掉这些参数重试一次，不再靠猜
                log(f"{label} 400 响应体: {resp.text[:200]!r}；移除思考参数后重试")
                payload.pop("thinking", None)
                payload.pop("reasoning_effort", None)
                thinking_removed = True
                attempt -= 1
                continue
            if resp.status_code != 200:
                # 任何 HTTP 错误都带响应体：bigmodel 的 body 含具体错误码
                # （1211 模型不存在 / 参数非法 / 1302 频控等），Actions 日志可直接定位
                log(f"{label} HTTP {resp.status_code} 响应体: {resp.text[:200]!r}")
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
                message = resp.json()["choices"][0]["message"]
                content = message.get("content") or ""
                reasoning = message.get("reasoning_content") or ""
            except (ValueError, KeyError, IndexError) as e:
                # HTTP 200 但 body 不是预期结构（网关故障页等）：
                # 把响应体片段带进异常消息，方便从 Actions 日志定位
                raise ValueError(f"GLM 响应结构异常（{e}），body 前 200 字符: {resp.text[:200]!r}")
            try:
                glossary = _parse_json_block(content)
            except ValueError:
                # 思考型模型兜底：content 为空/夹带思考文本时，尝试从
                # reasoning_content 里提取 JSON 对象
                if reasoning:
                    glossary = _parse_json_block(reasoning)
                else:
                    raise
            if not isinstance(glossary, dict):
                raise ValueError("LLM 输出不是 JSON 对象")
            return glossary
        except Exception as e:
            if attempt >= 3:
                log(f"{label} 第 {attempt} 次尝试失败（放弃）: {e}")
                break
            log(f"{label} 第 {attempt} 次尝试失败，1s 后重试: {e}")
            if isinstance(e, ValueError) and "GLM 响应结构异常" not in str(e):
                # 解析失败时留一段原始输出，方便从 Actions 日志定位模型行为
                raw = content if content else "(content 为空)"
                log(f"GLM 原始输出前 400 字符: {str(raw)[:400]!r}")
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


# ---------------------------------------------------------------------------
# 全局内建词表（历史剧集挖掘，周构建自生长）
# ---------------------------------------------------------------------------

GLOSSARY_TARGETED_PROMPT = """你是英语学习材料的编辑。下面是一段 BBC 6 Minute English 对白。
请只给出**指定词表**中每个词在这段对白语境中的中文释义（不超过 10 个字，简洁、易懂）。

仅输出一个 JSON 对象（键为单词、值为只含 meaning 的对象，全部用双引号），
不要输出任何其他文字，不要输出指定词表以外的词。示例格式：
{
  "单词": {"meaning": "不超过10字的中文释义"}
}
"""


def load_global_glossary() -> dict:
    if GLOSSARY_FILE.is_file():
        try:
            data = json.loads(GLOSSARY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError) as e:
            log(f"警告：全局词表 {GLOSSARY_FILE.name} 读取失败（{e}），按空表处理")
    return {}


def save_global_glossary(glossary: dict) -> None:
    """保存全局词表：键为清洗词（小写字母数字），含短语成分/连字符别名，
    排序保证确定性（沿用剧集词典的别名规则）。"""
    expanded: dict = {}
    for word, meaning in glossary.items():
        key = word.strip().lower()
        if key and meaning and key not in expanded:
            expanded[key] = meaning
    for phrase in list(expanded):
        # 短语成分别名（the grid → grid）与清洗别名（we've/life-saving → 前端 cleanWord 键）
        if " " in phrase:
            for part in WORD_RE.findall(phrase):
                if part not in STOPWORDS and part not in expanded:
                    expanded[part] = expanded[phrase]
        cleaned = clean_word(phrase)
        if cleaned and cleaned not in expanded:
            expanded[cleaned] = expanded[phrase]
    GLOSSARY_FILE.write_text(
        json.dumps(expanded, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def merge_glossary_entries(glossary: dict, entries: dict) -> int:
    """把 {词: 释义或 {meaning:...}} 并入全局词表（先到先得），返回新增数。"""
    added = 0
    for word, info in entries.items():
        meaning = info.get("meaning", "") if isinstance(info, dict) else str(info)
        key = word.strip().lower()
        if key and meaning and key not in glossary:
            glossary[key] = meaning
            added += 1
    return added


def build_glossary_from_history(n_episodes: int, api_key: str) -> int:
    """一次性挖掘：抓最近 n 期历史剧集（只取 transcript，不上货架），
    逐块向 LLM 索要「该块中尚未入库的词」的释义，并入全局词表。

    预过滤让后期块基本零新词、直接跳过——100 期实际 LLM 调用集中在前期。
    """
    episodes = parse_index(http_get(INDEX_URL))
    if not episodes:
        log("错误：索引页解析出 0 期剧集，放弃挖掘")
        return 1
    targets = episodes[:n_episodes]
    log(f"全局词表挖掘：目标 {len(targets)} 期历史剧集（现有词表 {len(load_global_glossary())} 词）")

    glossary = load_global_glossary()
    calls = skipped_chunks = 0
    for ei, meta in enumerate(targets, 1):
        try:
            episode = parse_episode(http_get(meta["url"]), meta)
        except Exception as e:
            log(f"[{ei}/{len(targets)}] {meta['id']} 解析失败，跳过: {e}")
            continue
        vocab_lines = "\n".join(
            f"{item.get('word', '')}: {item.get('definition', '')}".strip()
            for item in episode.get("vocabulary", []) if item.get("word")
        )
        for chunk in _chunk_transcript(episode["transcript"], GLOSSARY_CHUNK_WORDS):
            # 定向词表：该块中尚未入库的清洗词（含官方词汇表词优先保留原文键）
            wanted = []
            for token in chunk_text_tokens(chunk):
                if token not in glossary and token not in wanted:
                    wanted.append(token)
            if not wanted:
                skipped_chunks += 1
                continue
            parts = [GLOSSARY_TARGETED_PROMPT]
            if vocab_lines:
                parts.append(f"官方词汇表（全期通用）：\n{vocab_lines}")
            parts.append("指定词表（只输出这些词）：\n" + "、".join(wanted))
            parts.append("对白：\n" + "\n".join(chunk))
            part = _glm_glossary_call("\n\n".join(parts), api_key, f"挖掘 {meta['id']} 块")
            calls += 1
            if part:
                merge_glossary_entries(glossary, part)
        if ei % 10 == 0 or ei == len(targets):
            log(f"[{ei}/{len(targets)}] 累计词表 {len(glossary)} 词（LLM 调用 {calls} 次，"
                f"跳过 {skipped_chunks} 个无新词块）")

    save_global_glossary(glossary)
    log(f"全局词表完成：共 {len(glossary)} 词，LLM 调用 {calls} 次，已写入 {GLOSSARY_FILE.name}")
    return 0


def chunk_text_tokens(chunk: list[str]) -> list[str]:
    """块内出现的清洗词序列（保持出现顺序、去重）。"""
    tokens: list[str] = []
    for line in chunk:
        for token in line.split():
            cleaned = clean_word(token)
            if cleaned and cleaned not in tokens:
                tokens.append(cleaned)
    return tokens


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
    # 截断抢救：输出超长被 max_tokens 截断时（全词覆盖 + sentence 字段时期实测），
    # 丢弃残缺的尾条目、截到最后一个完整条目并补闭合括号——宁可少几个词也不整块作废
    cut = text.rfind("},")
    if start != -1 and cut > start and cut + 1 != end:
        candidates.append(text[start : cut + 1] + "}")
    last_error = None
    for candidate in candidates:
        try:
            return parse(candidate)
        except ValueError as e:
            last_error = e
    raise ValueError(f"输出中找不到可解析的 JSON 对象（{last_error}）")


EPISODE_TRANSLATIONS_PROMPT = """你是专业译者。下面是 BBC 6 Minute English 一期节目的标题与逐句对白
（已按句切分并编号）。请给出标题的中文翻译和每个句子的简洁中文翻译（口语化）。

仅输出一个 JSON 对象（不要任何其他文字），格式：
{"title": "标题中文翻译", "sentences": ["第1句中文", "第2句中文", ...]}
sentences 数组长度必须与输入句数一致。
"""


# 句边界 = 句末标点(可连续/省略号) + 可选收尾引号/括号 + 空白；切点在引号之后。
# BBC 大量使用 'Could vaccines cure cancer?' 式引号句，纯 (?<=[.!?])\s+ 会在 ? 与 ' 之间
# 漏切——与前端 JS 的 splitSentences() 同构（同正则同算法），保证数据 sentences
# 与前端逐词归属对齐。缩写（Mr. 等）容忍误差。
SENTENCE_BOUNDARY_RE = re.compile(r'([.!?]+["' + "'" + r'\u201d\u2019)]*)\s+')


def split_sentences(text: str) -> list[str]:
    parts, last = [], 0
    for m in SENTENCE_BOUNDARY_RE.finditer(text):
        parts.append(text[last : m.end(1)])
        last = m.end()
    parts.append(text[last:])
    return [p.strip() for p in parts if p.strip()]



def generate_episode_translations(title: str, sentences: list[str], api_key: str) -> dict:
    """一期一次 LLM 调用：标题 + 逐句中文翻译。失败返回 {}（前端不显示）。"""
    lines = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(sentences))
    prompt = (EPISODE_TRANSLATIONS_PROMPT + f"\n标题：{title}\n\n句子（共 {len(sentences)} 句）：\n{lines}")
    url = f"{LLM_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "user", "content": prompt},
            {"role": "system", "content": "You are a precise assistant that only outputs valid JSON."},
        ],
        "temperature": 0.2,
        "max_tokens": 4500,
    }
    if re.match(r"glm-(5\.3|4\.7)", LLM_MODEL):
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = "low"
    elif re.match(r"glm-(4\.[5-9]|[5-9])", LLM_MODEL):
        payload["thinking"] = {"type": "disabled"}
    content = None
    for attempt in (1, 2):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            if resp.status_code == 429:
                log(f"句子翻译 429 响应体: {resp.text[:200]!r}")
            resp.raise_for_status()
            try:
                message = resp.json()["choices"][0]["message"]
                content = message.get("content") or ""
                reasoning = message.get("reasoning_content") or ""
            except (ValueError, KeyError, IndexError) as e:
                raise ValueError(f"GLM 响应结构异常（{e}）")
            try:
                result = _parse_json_block(content)
            except ValueError:
                if reasoning:
                    result = _parse_json_block(reasoning)
                else:
                    raise
            if not isinstance(result, dict):
                raise ValueError("输出不是 JSON 对象")
            return result
        except Exception as e:
            log(f"句子翻译第 {attempt} 次尝试失败: {e}")
    return {}


def bake_sentences(episode: dict, translations: dict) -> list[dict]:
    """把切分结果与 LLM 翻译组装成 sentences: [{text, translation}]（对齐兜底）。"""
    sents = [{"text": s, "translation": ""} for s in split_sentences(episode["transcript"])]
    tr_list = translations.get("sentences")
    if isinstance(tr_list, list):
        for i, tr in enumerate(tr_list):
            if i < len(sents) and tr:
                sents[i]["translation"] = str(tr)
    return sents


def _parse_json_array_block(text: str) -> list:
    """容错提取 LLM 输出的 JSON 字符串数组（含截断抢救）。"""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE)

    def parse(candidate: str) -> list:
        try:
            v = json.loads(candidate)
            if isinstance(v, list):
                return v
        except json.JSONDecodeError:
            pass
        try:
            v = ast.literal_eval(candidate)
            if isinstance(v, list):
                return v
        except (ValueError, SyntaxError, TypeError):
            pass
        raise ValueError("无法解析为 JSON 数组")

    candidates = [text]
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    cut = text.rfind('",')
    if cut > start:
        candidates.append(text[start : cut + 1] + "]")
    for c in candidates:
        try:
            return parse(c)
        except ValueError:
            continue
    raise ValueError("输出中找不到可解析的 JSON 数组")


def load_basics_keys() -> set:
    """前端基础词表键（docs/assets/basics.json）——已覆盖词，无需 LLM。"""
    p = Path(__file__).resolve().parent / "docs" / "assets" / "basics.json"
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()


def generate_glossary_for_episode(episode: dict, covered: set, api_key: str) -> dict:
    """优先使用全局词表缓存，LLM 只补缺失词（定向词表逐块请求）。

    官方词汇表词始终交给 LLM（数量少、且需要语境中文化，英文官方释义不如中文）。
    返回 {词: {meaning}}（仅缺失词 + 官方词汇词的语境释义）。
    """
    vocab_words = {item.get("word", "").strip().lower()
                   for item in episode.get("vocabulary", []) if item.get("word")}
    calls = skipped = 0
    glossary: dict = {}
    vocab_lines = "\n".join(
        f"{item.get('word', '')}: {item.get('definition', '')}".strip()
        for item in episode.get("vocabulary", []) if item.get("word")
    )
    for chunk in _chunk_transcript(episode["transcript"], GLOSSARY_CHUNK_WORDS):
        wanted = []
        for token in chunk_text_tokens(chunk):
            if (token not in covered and token not in wanted) and (
                token in vocab_words or clean_word(token) not in covered
            ):
                # 官方词汇表词的原文键（可能带短语/连字符）也收
                wanted.append(token)
        if not wanted:
            skipped += 1
            continue
        parts = [GLOSSARY_TARGETED_PROMPT]
        if vocab_lines:
            parts.append(f"官方词汇表（全期通用）：\n{vocab_lines}")
        parts.append("指定词表（只输出这些词）：\n" + "、".join(wanted))
        parts.append("对白：\n" + "\n".join(chunk))
        part = _glm_glossary_call("\n\n".join(parts), api_key, f"补词 {episode['id']}")
        calls += 1
        if part:
            merge_glossary_entries(glossary, part)
    log(f"词表缓存优先：跳过 {skipped} 个无缺词块，LLM 补 {len(glossary)} 词（{calls} 次调用）")
    return glossary


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

    for token in episode["title"].split():
        remember(token)  # 标题词也要发音（标题参与打字，完词即播）
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
    global_glossary = load_global_glossary()  # 词表缓存：新剧集优先复用，LLM 只补缺
    basics_keys = load_basics_keys()
    for meta in recent:
        out_file = DATA_DIR / f"{meta['id']}.json"
        existing: dict | None = None
        if out_file.exists():
            try:
                loaded = json.loads(out_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (json.JSONDecodeError, OSError):
                existing = None
            # 已存在的剧集原则上跳过（幂等），两种情况除外：
            # ① 本地文件半损坏（构建中断截断等）：当场重抓覆盖——否则下方 prune
            #    会把它删掉，该期剧集要空窗到下次构建，且日志误记为「已在架跳过」
            # ② 此前构建时 GLM 不可用/失败，context_glossary 为空且本次有 key：
            #    补烘语境词表并重建词典，避免「一次降级被永久固化」
            if existing is None:
                log(f"本地文件 {out_file.name} 损坏/不可读，重新抓取")
            elif not no_llm and api_key and "transcript" in existing \
                    and not existing.get("context_glossary"):
                log(f"补烘语境词表：{meta['id']}（此前构建无 GLM 结果）")
                covered = set(global_glossary) | basics_keys
                glossary = generate_glossary_for_episode(existing, covered, api_key)
                if glossary:
                    existing["context_glossary"] = glossary
                    existing["dict"] = bake_word_dict(existing)
                    out_file.write_text(
                        json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    rebaked += 1
                    log(f"已更新 {out_file.name}，词典 {len(existing['dict'])} 条")
                else:
                    skipped += 1  # 补烘失败仍留在架，别让汇总把它算成「新增」
                continue
            elif not no_llm and api_key and existing and "transcript" in existing \
                    and (not existing.get("sentences")
                         or not any(s.get("translation") for s in existing.get("sentences") or [])
                         or [s.get("text") for s in existing.get("sentences") or []]
                            != split_sentences(existing["transcript"])):
                # 老数据升级：翻译缺失，或句子切分与当前切分器不一致（切分规则
                # 迭代过引号感知版本）→ 按句子粒度重新生成（含标题翻译）
                log(f"补句子翻译：{meta['id']}（缺失/切分漂移）")
                translations = generate_episode_translations(
                    existing.get("title", ""),
                    split_sentences(existing["transcript"]),
                    api_key,
                )
                existing["title_translation"] = str(translations.get("title", ""))
                existing["sentences"] = bake_sentences(existing, translations)
                existing.pop("turn_translations", None)  # 旧段落粒度字段退役
                if any(s["translation"] for s in existing["sentences"]):
                    out_file.write_text(
                        json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    translated_n = sum(1 for s in existing["sentences"] if s["translation"])
                    log(f"已补 {out_file.name} 句子翻译 {translated_n} 句 + 标题")
                skipped += 1
                continue
            else:
                if not no_llm and api_key:
                    log(f"跳过已存在剧集：{meta['id']}"
                        f"（语境词表 {len(existing.get('context_glossary') or {})} 条，非空不再重新生成）")
                else:
                    log(f"跳过已存在剧集：{meta['id']}")
                skipped += 1
                continue
        try:
            episode = parse_episode(http_get(meta["url"]), meta)
            if not no_llm and api_key:
                # 优先使用全局词表缓存，LLM 只补缺失词 + 官方词汇表中文化
                covered = set(global_glossary) | basics_keys
                episode["context_glossary"] = generate_glossary_for_episode(
                    episode, covered, api_key
                )
                translations = generate_episode_translations(
                    episode["title"],
                    split_sentences(episode["transcript"]),
                    api_key,
                )
                episode["title_translation"] = str(translations.get("title", ""))
                episode["sentences"] = bake_sentences(episode, translations)
                if not episode["title_translation"] and not any(
                    s["translation"] for s in episode["sentences"]
                ):
                    log(f"警告：{meta['id']} 句子翻译生成失败，前端将不显示整句翻译")
            else:
                episode["context_glossary"] = {}
                episode["title_translation"] = ""
                episode["sentences"] = bake_sentences(episode, {})
            episode["dict"] = bake_word_dict(episode)
            out_file.write_text(
                json.dumps(episode, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            translated_n = sum(1 for s in episode["sentences"] if s["translation"])
            log(f"已写入 {out_file.name}：{len(episode['transcript'].split())} 词对白，"
                f"词典 {len(episode['dict'])} 条，句子 {translated_n}/{len(episode['sentences'])} 句已译")
        except Exception as e:
            failures += 1
            log(f"剧集 {meta['id']} 处理失败: {e}")
    log(f"本次构建：新增 {len(recent) - skipped - rebaked - failures} 期，"
        f"补烘 {rebaked} 期，跳过（已在架）{skipped} 期")

    # 全局词表自生长：本期新生成的语境词表并入 glossary.json（先到先得）
    grew = 0
    for meta in recent:
        f = DATA_DIR / f"{meta['id']}.json"
        if not f.is_file():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            grew += merge_glossary_entries(global_glossary, data.get("context_glossary") or {})
        except (json.JSONDecodeError, OSError):
            continue
    if grew:
        save_global_glossary(global_glossary)
        log(f"全局词表自生长：新增 {grew} 词，共 {len(global_glossary)} 词")

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
            # 索引必需字段缺失同样按损坏处理：在这里探测走删除分支，
            # 而不是等到下方索引构建时 KeyError 炸掉整个构建
            missing = [k for k in ("id", "title", "pub_date", "bbc_url") if k not in data]
            if missing:
                raise KeyError(f"缺少字段 {missing}")
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
    parser.add_argument("--build-glossary", type=int, metavar="N", default=0,
                        help="一次性挖掘最近 N 期历史剧集构建全局词表（只取 transcript，不上货架）")
    args = parser.parse_args()
    try:
        if args.build_glossary > 0:
            api_key = os.environ.get("GLM_API_KEY", "")
            if not api_key:
                log("错误：--build-glossary 需要 GLM_API_KEY")
                return 1
            return build_glossary_from_history(args.build_glossary, api_key)
        return build(args.days, args.no_llm, args.no_audio, args.rebake)
    except requests.RequestException as e:
        log(f"网络请求失败（索引页不可达？）: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
