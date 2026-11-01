"""fetcher 解析器与滚动窗口逻辑测试。

fixture 为 2026-10-29 / 2026-10-22 两期真实剧集页与栏目索引页的原始 HTML
（tests/fixtures/，经代理下载）。BBC 改版时这些断言会失败，提示更新解析器。
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fetcher  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def index_episodes():
    html = (FIXTURES / "index.html").read_text(encoding="utf-8")
    return fetcher.parse_index(html)


@pytest.fixture(scope="module")
def episode_261029(index_episodes):
    meta = next(e for e in index_episodes if e["id"] == "261029")
    html = (FIXTURES / "episode-261029.html").read_text(encoding="utf-8")
    return fetcher.parse_episode(html, meta)


# ---------------------------------------------------------------- 索引页


def test_parse_index_dedup_and_count(index_episodes):
    ids = [e["id"] for e in index_episodes]
    assert len(ids) == len(set(ids)), "剧集应按 id 去重"
    assert len(index_episodes) > 100, "索引页应解析出大量历史剧集"


def test_parse_index_fields(index_episodes):
    ep = index_episodes[0]
    assert ep == {
        "id": "261029",
        "ep_num": "261029",
        "url": "https://www.bbc.co.uk/learningenglish/english/features/"
        "6-minute-english_2026/ep-261029",
        "title": "Is aircon the answer to a heating world?",
        "pub_date": "2026-10-29",
    }


def test_parse_index_date_decoding(index_episodes):
    by_id = {e["id"]: e for e in index_episodes}
    assert by_id["261022"]["pub_date"] == "2026-10-22"
    assert by_id["261015"]["pub_date"] == "2026-10-15"


# ---------------------------------------------------------------- 剧集页


def test_episode_meta(episode_261029):
    assert episode_261029["title"] == "Is aircon the answer to a heating world?"
    assert episode_261029["pub_date"] == "2026-10-29"
    assert episode_261029["bbc_url"].endswith("ep-261029")
    assert episode_261029["mp3_url"].startswith("https://downloads.bbc.co.uk/")
    assert episode_261029["mp3_url"].endswith(".mp3")


def test_episode_vocabulary(episode_261029):
    words = [v["word"] for v in episode_261029["vocabulary"]]
    assert "life-saving" in words
    assert "the grid" in words
    lifesaving = next(v for v in episode_261029["vocabulary"] if v["word"] == "life-saving")
    assert "dying" in lifesaving["definition"]


def test_episode_transcript_shape(episode_261029):
    t = episode_261029["transcript"]
    assert len(t.split()) > 800, "整期对白应有 800+ 词"
    lines = [ln for ln in t.split("\n") if ln.strip()]
    assert len(lines) > 30, "对白回合数应大于 30"
    assert lines[0].startswith("Hi"), "说话人名应被剥离，首行直接是台词"


def test_episode_transcript_no_junk(episode_261029):
    t = episode_261029["transcript"]
    assert "TRANSCRIPT" not in t
    assert "not a word-for-word" not in t, "官方 Note 行应被剔除"
    assert "What do you think" not in t, "页脚应被剔除"
    assert "A-Z list" not in t
    assert "Georgie\n" not in t, "说话人名不应单独成行"


def test_episode_transcript_inline_emphasis_kept(episode_261029):
    """句中 <strong> 强调的词汇（如 shading）应保留词本身而非被丢弃。"""
    assert "And shading is blocking" in episode_261029["transcript"]


def test_episode_turns_structure(episode_261029):
    """对话回合：说话人归属正确（含嘉宾），transcript == 回合文本按行拼接。"""
    turns = episode_261029["turns"]
    assert len(turns) > 30
    assert turns[0] == {"speaker": "Georgie", "text": "Hi Neil."}
    assert turns[1]["speaker"] == "Neil"
    speakers = {t["speaker"] for t in turns}
    assert "Dr Radhika Khosla" in speakers, "嘉宾说话人应正确归属"
    # flat transcript 与回合的一致性
    assert episode_261029["transcript"] == "\n".join(t["text"] for t in turns)
    # 说话人名只出现在徽章，不进入文本流
    assert all(not t["text"].startswith(t["speaker"]) for t in turns)


def test_second_episode_parses(index_episodes):
    meta = next(e for e in index_episodes if e["id"] == "261022")
    html = (FIXTURES / "episode-261022.html").read_text(encoding="utf-8")
    ep = fetcher.parse_episode(html, meta)
    assert ep["title"].startswith("What should you eat")
    assert len(ep["transcript"].split()) > 800
    assert len(ep["vocabulary"]) >= 4


# ---------------------------------------------------------------- 工具函数


def test_clean_word():
    assert fetcher.clean_word("Life-Saving.") == "lifesaving"
    assert fetcher.clean_word("don't") == "dont"
    assert fetcher.clean_word("(the)") == "the"


def test_parse_json_block_variants():
    assert fetcher._parse_json_block('{"a": 1}') == {"a": 1}
    assert fetcher._parse_json_block('```json\n{"a": 1}\n```') == {"a": 1}
    assert fetcher._parse_json_block('结果如下：{"a": 1} 请查收') == {"a": 1}


def test_parse_json_block_single_quoted_python_repr():
    """实测（2026-11 Actions 运行）：glm-4-flash 偶尔无视提示输出
    单引号 Python 风格字典，json.loads 报
    "Expecting property name enclosed in double quotes: line 1 column 2"。
    降级链必须能用 ast.literal_eval 解回。"""
    raw = "{'inhabit': {'meaning': '（大量人）居住在', 'sentence': 'inhabit hospitals'}, 'the grid': {\"meaning\": '电网', 'sentence': 'the grid'}}"
    assert fetcher._parse_json_block(raw) == {
        "inhabit": {"meaning": "（大量人）居住在", "sentence": "inhabit hospitals"},
        "the grid": {"meaning": "电网", "sentence": "the grid"},
    }


def test_parse_json_block_trailing_comma():
    # Python 字面量容忍尾逗号（json 不容忍）
    assert fetcher._parse_json_block('{"a": {"m": "x",},}') == {"a": {"m": "x"}}


def test_parse_json_block_garbage_raises():
    import pytest as _pytest
    with _pytest.raises(ValueError):
        fetcher._parse_json_block("完全不是结构化输出的文本")


def test_glossary_prompt_braces_are_single():
    """回归测试（根因）：GLOSSARY_PROMPT 是普通字符串，历史上误写成 f-string 的
    {{ 转义，模型照抄双层花括号输出，导致两轮线上解析失败
    （Expecting property name / unhashable type: 'dict'）。"""
    assert "{{" not in fetcher.GLOSSARY_PROMPT
    assert "}}" not in fetcher.GLOSSARY_PROMPT
    assert '{"meaning"' in fetcher.GLOSSARY_PROMPT  # 示例本身是合法 JSON 片段


# ---------------------------------------------------------------- ECDICT 表名兼容


def _make_dict_db(path, table):
    import sqlite3
    conn = sqlite3.connect(str(path))
    conn.execute(
        f"CREATE TABLE {table} (word TEXT, translation TEXT)"
    )
    conn.execute(f"INSERT INTO {table} VALUES ('summer', 'n. 夏季')")
    conn.commit()
    conn.close()


def test_detect_ecdict_table_both_schemas(tmp_path):
    """本地 ecdict.sqlite 用 entries 表，ECDICT Release 的 stardict.db 用 stardict 表
    （列名相同）——历史 bug：只查 entries，Actions 里整层词典静默丢失。"""
    for name, table in [("local.sqlite", "entries"), ("release.sqlite", "stardict")]:
        db = tmp_path / name
        _make_dict_db(db, table)
        assert fetcher.detect_ecdict_table(db) == table
    # 无可用表 → None
    empty = tmp_path / "empty.sqlite"
    import sqlite3
    sqlite3.connect(str(empty)).close()
    assert fetcher.detect_ecdict_table(empty) is None


def test_lookup_ecdict_with_table(tmp_path):
    db = tmp_path / "stardict.db"
    _make_dict_db(db, "stardict")
    result = fetcher.lookup_ecdict(["summer", "missing"], db, "stardict")
    assert result == {"summer": "n. 夏季"}


def test_find_ecdict_size_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "ECDICT_MIN_BYTES", 1)  # 测试库只有几 KB
    garbage = tmp_path / "garbage.sqlite"
    garbage.write_bytes(b"x" * 2_000_000)  # 够大但不是 sqlite
    valid = tmp_path / "ecdict.sqlite"
    _make_dict_db(valid, "entries")
    # 大小门槛：太小的不看；内容不是 sqlite 的告警忽略
    monkeypatch.setattr(fetcher, "ECDICT_CANDIDATES", [tmp_path / "tiny.sqlite"])
    (tmp_path / "tiny.sqlite").write_bytes(b"x" * 100)
    assert fetcher.find_ecdict() is None
    monkeypatch.setattr(fetcher, "ECDICT_CANDIDATES", [garbage])
    assert fetcher.find_ecdict() is None
    monkeypatch.setattr(fetcher, "ECDICT_CANDIDATES", [valid])
    assert fetcher.find_ecdict() == (valid, "entries")


# ------------------------------------------------- GLM 调用链（本地模拟服务）

def _mock_glm_server(content: str):
    """起本地 HTTP 服务模拟 GLM chat/completions，POST 一律返回固定 content。

    走真实 requests.post → 解析 全链路，无需 API key。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.dumps(
                {"choices": [{"message": {"content": content}}]}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_generate_context_glossary_output_variants(monkeypatch):
    """用模拟 GLM 服务验证：历史实测出现过的四种输出形态都能解析成功。"""
    expected = {"hi": {"meaning": "打招呼", "sentence": "Hi Neil."}}
    cases = {
        "标准 JSON": '{"hi": {"meaning": "打招呼", "sentence": "Hi Neil."}}',
        "单引号 Python repr（实测 2026-11-01）": "{'hi': {'meaning': '打招呼', 'sentence': 'Hi Neil.'}}",
        "双层花括号照抄畸形示例（实测 2026-11-01）": '{{"hi": {"meaning": "打招呼", "sentence": "Hi Neil."}}}',
        "双层花括号+单引号": "{{'hi': {'meaning': '打招呼', 'sentence': 'Hi Neil.'}}}",
        "围栏+解说文字": '好的，结果：```json\n{"hi": {"meaning": "打招呼", "sentence": "Hi Neil."}}\n```请查收',
    }
    for name, content in cases.items():
        server = _mock_glm_server(content)
        try:
            monkeypatch.setattr(
                fetcher, "LLM_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}/v1"
            )
            result = fetcher.generate_context_glossary("Hi Neil. How are you?", "test-key")
            assert result == expected, f"{name}: {result}"
        finally:
            server.shutdown()
            server.server_close()


def test_generate_context_glossary_garbage_degrades(monkeypatch):
    """完全无法解析时返回 {}（降级不炸构建），重试后仍失败。"""
    server = _mock_glm_server("我觉得这个对白没什么难词，不需要词表。")
    try:
        monkeypatch.setattr(
            fetcher, "LLM_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}/v1"
        )
        assert fetcher.generate_context_glossary("Hi Neil.", "test-key") == {}
    finally:
        server.shutdown()
        server.server_close()


def test_bake_word_dict_priority_and_alias():
    episode = {
        "transcript": "The grid supplies power. Life-saving machines inhabit hospitals.",
        "vocabulary": [{"word": "the grid", "definition": "the electricity network"}],
        "context_glossary": {
            "inhabit": {"meaning": "（大量人）居住在", "sentence": "inhabit hospitals"},
            "the grid": {"meaning": "（国家的）电网", "sentence": "s"},
        },
    }
    baked = fetcher.bake_word_dict(episode, None)

    # 优先级：LLM 语境 > 官方 > 词典（同一词两层都有时语境胜出）
    assert baked["the grid"]["source"] == "context"
    assert baked["the grid"]["context"] == "（国家的）电网"
    # 短语成分别名继承胜出层（前端按空白分词，只能查到 grid）
    assert baked["grid"]["source"] == "context"
    assert baked["grid"]["phrase"] == "the grid"
    # 连字符别名：前端 cleanWord("life-saving") == "lifesaving"
    assert "lifesaving" not in baked, "无来源覆盖的词不应凭空出现"
    # 语境层
    assert baked["inhabit"]["source"] == "context"
    assert baked["inhabit"]["context"] == "（大量人）居住在"
    assert baked["inhabit"]["sentence"] == "inhabit hospitals"
    assert baked["inhabit"]["translation"] == "（大量人）居住在"


def test_bake_word_dict_context_overrides_official():
    """用户指定的优先级：同一词 LLM 语境释义覆盖 BBC 官方英文释义。"""
    episode = {
        "transcript": "shading blocks the sun",
        "vocabulary": [{"word": "shading", "definition": "official def"}],
        "context_glossary": {"shading": {"meaning": "遮阳", "sentence": "s"}},
    }
    baked = fetcher.bake_word_dict(episode, None)
    assert baked["shading"]["source"] == "context"
    assert baked["shading"]["translation"] == "遮阳"


def test_bake_word_dict_alias_order_deterministic():
    """别名必须按固定（排序）顺序插入：set 迭代顺序受 Python 哈希随机化影响，
    不排序会导致同一剧集跨进程两次构建字节不同，破坏「产物确定性 → Actions
    无空提交」的前提（JSON 按插入序序列化）"""
    episode = {
        "transcript": "societal pressure pushes",
        "vocabulary": [{"word": "societal pressure", "definition": "d"}],
        "context_glossary": {},
    }
    baked = fetcher.bake_word_dict(episode, None)
    assert list(baked) == [
        "societal pressure",  # 官方词汇表短语本体
        "pressure",           # 成分别名（排序后依次插入）
        "societal",
        "societalpressure",   # 连字符清洗别名
    ]


# ---------------------------------------------------------------- TTS 音频


def test_collect_words_represents_original_spelling():
    """音频键用清洗词（与前端文件名一致），值保留原文写法给 TTS（连写词读不准）。"""
    episode = {
        "transcript": "We've had the six-minute quiz. we've",
        "vocabulary": [{"word": "the grid", "definition": "d"}],
        "context_glossary": {"inhabit": {"meaning": "x", "sentence": "s"}},
    }
    words = fetcher.collect_words(episode)
    assert words["weve"] == "We've"      # 首次出现的原文，而非第二次的 we've
    assert words["sixminute"] == "six-minute"
    assert words["grid"] == "grid"       # 词汇表短语取成分词（TTS 文本为单词本身）
    assert words["thegrid"] == "the grid"  # 短语连写键，代表原文为短语
    assert words["inhabit"] == "inhabit"  # 语境词表键
    assert "the" in words                 # 虚词也在对白里，需要发音


def test_prune_audio_removes_unreferenced(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "AUDIO_DIR", tmp_path)
    for name in ("hi.mp3", "old.mp3", ".stale.part"):
        (tmp_path / name).write_bytes(b"x")
    removed = fetcher.prune_audio({"hi"})
    assert removed == 2
    assert (tmp_path / "hi.mp3").exists()
    assert not (tmp_path / "old.mp3").exists()
    assert not (tmp_path / ".stale.part").exists()


def test_prune_audio_no_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "AUDIO_DIR", tmp_path / "nonexistent")
    assert fetcher.prune_audio(set()) == 0


# ---------------------------------------------------------------- rebake / TTS


def test_rebake_rebuilds_dict_keeps_glossary(tmp_path, monkeypatch):
    """rebake：重建词典（ECDICT 层恢复）、保留 context_glossary 与 transcript、
    不触网（独立模式在 parse_index 之前返回）。"""
    import os
    data_dir = tmp_path / "episodes"
    data_dir.mkdir()
    episode = {
        "id": "261001", "title": "T", "pub_date": "2026-10-01",
        "bbc_url": "https://example.com", "transcript": "summer heat",
        "vocabulary": [], "context_glossary": {"heat": {"meaning": "高温", "sentence": "s"}},
        "dict": {"onlyofficial": {"translation": "x", "source": "official"}},
    }
    (data_dir / "261001.json").write_text(json.dumps(episode, ensure_ascii=False), encoding="utf-8")

    db = tmp_path / "ecdict.sqlite"
    _make_dict_db(db, "entries")
    monkeypatch.setattr(fetcher, "DATA_DIR", data_dir)
    monkeypatch.setattr(fetcher, "ECDICT_MIN_BYTES", 1)
    monkeypatch.setattr(fetcher, "ECDICT_CANDIDATES", [db])
    monkeypatch.setattr(fetcher, "AUDIO_DIR", tmp_path / "audio")  # rebake 不碰音频

    rc = fetcher.build(days=30, no_llm=True, no_audio=True, rebake=True)
    assert rc == 0
    rebaked = json.loads((data_dir / "261001.json").read_text(encoding="utf-8"))
    assert rebaked["context_glossary"] == episode["context_glossary"], "语境词表必须保留"
    assert rebaked["transcript"] == episode["transcript"]
    assert rebaked["dict"]["summer"]["source"] == "ecdict", "ECDICT 层必须恢复"
    assert rebaked["dict"]["heat"]["source"] == "context", "语境层优先于词典层"
    assert "onlyofficial" not in rebaked["dict"], "旧词典整体重建而非合并"


def test_generate_missing_audio_incremental(tmp_path, monkeypatch):
    """TTS 音频：跳过已有、只补缺失；临时文件不残留。"""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    (audio_dir / "hi.mp3").write_bytes(b"old")
    (audio_dir / ".stale.part").write_bytes(b"x")
    monkeypatch.setattr(fetcher, "AUDIO_DIR", audio_dir)

    saved_paths = []

    class FakeCommunicate:
        def __init__(self, text, voice):
            self.text = text

        async def save(self, path):
            saved_paths.append(path)
            import pathlib
            pathlib.Path(path).write_bytes(b"mp3")

    monkeypatch.setattr(fetcher.edge_tts, "Communicate", FakeCommunicate)

    generated, failed = fetcher.generate_missing_audio([{
        "transcript": "Hi hot summer",   # hi 已有；hot/summer 待生成
        "vocabulary": [],
        "context_glossary": {},
    }])
    assert (generated, failed) == (2, 0)
    assert (audio_dir / "hi.mp3").read_bytes() == b"old", "已有音频不重新生成"
    assert (audio_dir / "hot.mp3").exists() and (audio_dir / "summer.mp3").exists()
    # TTS 文本用原文写法（cleaned 键 -> 代表原文）
    assert not list(audio_dir.glob("*.part")), "临时文件必须清理"


# ---------------------------------------------------------------- 滚动窗口


def _write_episode(data_dir: Path, ep_id: str, pub_date: str):
    data = {
        "id": ep_id,
        "title": f"Episode {ep_id}",
        "pub_date": pub_date,
        "bbc_url": "https://example.com",
        "transcript": "x",
        "vocabulary": [],
        "context_glossary": {},
        "dict": {},
    }
    (data_dir / f"{ep_id}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def test_prune_and_reindex(tmp_path):
    # 窗口 [10-27, 11-02]：261029 保留，261022 过期删除，损坏文件删除
    _write_episode(tmp_path, "261029", "2026-10-29")
    _write_episode(tmp_path, "261022", "2026-10-22")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

    kept = fetcher.prune_and_reindex(tmp_path, date(2026, 10, 27))

    assert [d["id"] for d in kept] == ["261029"]
    assert not (tmp_path / "261022.json").exists()
    assert not (tmp_path / "broken.json").exists()
    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    assert [e["id"] for e in index] == ["261029"]
    assert set(index[0]) == {"id", "title", "pub_date", "bbc_url"}


def test_prune_and_reindex_sorted_desc(tmp_path):
    _write_episode(tmp_path, "261022", "2026-10-22")
    _write_episode(tmp_path, "261029", "2026-10-29")
    _write_episode(tmp_path, "261026", "2026-10-26")
    kept = fetcher.prune_and_reindex(tmp_path, date(2026, 1, 1))
    assert [d["id"] for d in kept] == ["261029", "261026", "261022"]


def test_prune_and_reindex_deterministic(tmp_path):
    """两次运行产物必须完全一致（否则 Actions 每天产生空提交）。"""
    _write_episode(tmp_path, "261029", "2026-10-29")
    fetcher.prune_and_reindex(tmp_path, date(2026, 10, 27))
    first = (tmp_path / "index.json").read_text(encoding="utf-8")
    fetcher.prune_and_reindex(tmp_path, date(2026, 10, 27))
    assert (tmp_path / "index.json").read_text(encoding="utf-8") == first
