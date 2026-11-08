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


def test_parse_json_block_truncated_salvage():
    """截断抢救（实测 2026-11-03：全词覆盖后输出超 max_tokens 被切断，
    完美 JSON 前缀整体解析失败）：丢弃残缺尾条目、补闭合括号，保住完整条目。"""
    truncated = (
        '{\n  "hi": {"meaning": "你好"},\n  "so": {"meaning": "那么"},\n'
        '  "just": {"meaning": "刚'
    )
    assert fetcher._parse_json_block(truncated) == {
        "hi": {"meaning": "你好"},
        "so": {"meaning": "那么"},
    }


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


def test_glossary_prompt_requires_concise_meaning():
    """回归测试：① meaning ≤10 字硬性约束；② 全词覆盖（含 the/and 等基础词，
    学习者要求所有词都有解释——曾长期排除基础词导致删词典层后满屏无释义）。
    两个提示词都要受保护。"""
    for prompt in (fetcher.GLOSSARY_PROMPT, fetcher.GLOSSARY_PROMPT_NO_VOCAB):
        assert "10" in prompt and "简洁" in prompt
        assert "{{" not in prompt and "}}" not in prompt
        assert '{"meaning"' in prompt
        assert "每一个单词" in prompt and "基础" in prompt, "必须要求全词覆盖"
        assert "宁多勿漏" not in prompt and "不需要收录" not in prompt, "不得再排除基础词"


def test_generate_context_glossary_prompt_contains_vocab(monkeypatch):
    """有官方词汇表时，Prompt 必须拼入词汇表内容（供 LLM 参照转成中文）。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            captured["body"] = self.rfile.read(length).decode("utf-8")
            body = json.dumps(
                {"choices": [{"message": {"content": '{"hi": {"meaning": "你好", "sentence": "s"}}'}}]}
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
    try:
        monkeypatch.setattr(fetcher, "LLM_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}/v1")
        vocab = [
            {"word": "the grid", "definition": "the electricity network"},
            {"word": "", "definition": "空词应被过滤"},
        ]
        result = fetcher.generate_context_glossary("Hi Neil.", vocab, "test-key")
        assert result == {"hi": {"meaning": "你好", "sentence": "s"}}
        prompt = json.loads(captured["body"])["messages"][0]["content"]
        assert "the grid: the electricity network" in prompt, "词汇表内容必须进入 Prompt"
        assert "官方词汇表" in prompt and "Hi Neil." in prompt
        assert "空词应被过滤" not in prompt, "空 word 条目应被跳过"
    finally:
        server.shutdown()
        server.server_close()


def test_chunk_transcript_by_turn_boundaries():
    """分块：按行（回合）切分、不切断回合、块大小受限。"""
    turns = ["a " * 100] * 7  # 7 回合各 100 词
    transcript = "\n".join(t.strip() for t in turns)
    chunks = fetcher._chunk_transcript(transcript, chunk_words=250)
    assert [len(c) for c in chunks] == [2, 2, 2, 1]  # 100+100 / 100+100 / 100+100 / 100
    assert all(len(" ".join(c).split()) <= 250 for c in chunks)
    # 每行保持完整
    assert sum(len(c) for c in chunks) == 7
    short = fetcher._chunk_transcript("Hi there.", chunk_words=250)
    assert short == [["Hi there."]]


def test_glm_call_429_backoff(monkeypatch):
    """429 限流：指数退避后重试成功（不消耗解析重试次数），等待时间符合退避序列。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    state = {"calls": 0}
    waits = []
    monkeypatch.setattr(fetcher.time, "sleep", lambda s: waits.append(s))

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["calls"] += 1
            if state["calls"] <= 2:
                self.send_response(429)
                self.send_header("Retry-After", "3")
                body = b"{}"
            else:
                body = json.dumps(
                    {"choices": [{"message": {"content": '{"hi": {"meaning": "喂", "sentence": "s"}}'}}]}
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
    try:
        monkeypatch.setattr(fetcher, "LLM_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}/v1")
        result = fetcher._glm_glossary_call("prompt", "key", "测试块")
        assert result == {"hi": {"meaning": "喂", "sentence": "s"}}
        assert state["calls"] == 3, "两次 429 后第三次成功"
        # Retry-After: 3 优先于指数序列
        assert waits == [3, 3], f"退避等待应为两次 Retry-After=3：{waits}"
    finally:
        server.shutdown()
        server.server_close()


def test_glm_call_thinking_params_by_model(monkeypatch):
    """思考参数按官方文档分档：
    - glm-5.3/5.3-flash 恒定思考：thinking=enabled + reasoning_effort=low（独立字符串参数）
    - 其余 glm-4.5+/5.x：thinking=disabled
    - 旧模型（glm-4-flash 等）：不带思考参数"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            bodies.append(json.loads(self.rfile.read(length).decode("utf-8")))
            resp = json.dumps(
                {"choices": [{"message": {"content": '{"a": {"meaning": "x", "sentence": "s"}}'}}]}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}/v1"
        monkeypatch.setattr(fetcher, "LLM_BASE_URL", base)
        expected = {"a": {"meaning": "x", "sentence": "s"}}
        for model, want in [
            ("glm-5.3-flash", {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}),
            ("glm-4.6-flash", {"thinking": {"type": "disabled"}}),
            ("glm-4-flash", {}),
        ]:
            monkeypatch.setattr(fetcher, "LLM_MODEL", model)
            assert fetcher._glm_glossary_call("p", "k", "t") == expected
            sent = {k: v for k, v in bodies[-1].items() if k in ("thinking", "reasoning_effort")}
            assert sent == want, f"{model}: {sent}"
    finally:
        server.shutdown()
        server.server_close()


def test_glm_call_reasoning_content_fallback(monkeypatch):
    """思考型模型兜底：content 为空时从 reasoning_content 提取 JSON。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            resp = json.dumps({"choices": [{"message": {
                "content": "",
                "reasoning_content": '思考过程……最终结果：{"hi": {"meaning": "喂", "sentence": "s"}}',
            }}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(fetcher, "LLM_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}/v1")
        monkeypatch.setattr(fetcher, "LLM_MODEL", "glm-5.3-flash")
        result = fetcher._glm_glossary_call("p", "k", "t")
        assert result == {"hi": {"meaning": "喂", "sentence": "s"}}
    finally:
        server.shutdown()
        server.server_close()


def test_glm_call_400_drops_thinking_param(monkeypatch):
    """400 时自适应：思考参数被拒 → 打印响应体并全部移除后重试成功。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            bodies.append(json.loads(self.rfile.read(length).decode("utf-8")))
            if "thinking" in bodies[-1] or "reasoning_effort" in bodies[-1]:
                resp = json.dumps({"error": {"code": "1210", "message": "invalid parameter"}}).encode()
                self.send_response(400)
            else:
                resp = json.dumps(
                    {"choices": [{"message": {"content": '{"a": {"meaning": "x", "sentence": "s"}}'}}]}
                ).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(fetcher, "LLM_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}/v1")
        monkeypatch.setattr(fetcher, "LLM_MODEL", "glm-5.3-flash")
        result = fetcher._glm_glossary_call("p", "k", "t")
        assert result == {"a": {"meaning": "x", "sentence": "s"}}
        assert len(bodies) == 2
        assert "thinking" in bodies[0] and "reasoning_effort" in bodies[0]
        assert "thinking" not in bodies[1] and "reasoning_effort" not in bodies[1]
    finally:
        server.shutdown()
        server.server_close()


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
            result = fetcher.generate_context_glossary("Hi Neil. How are you?", [], "test-key")
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
        assert fetcher.generate_context_glossary("Hi Neil.", [], "test-key") == {}
    finally:
        server.shutdown()
        server.server_close()


def test_generate_context_glossary_chunked_and_merged(monkeypatch):
    """整期分块生成：多块各返回不同词条，合并去重且后块携带「已译词」名单。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    prompts = []
    replies = [
        '{"hot": {"meaning": "热", "sentence": "s1"}, "summer": {"meaning": "夏天", "sentence": "s2"}}',
        '{"hot": {"meaning": "重复应被忽略", "sentence": "x"}, "grid": {"meaning": "电网", "sentence": "s3"}}',
    ]
    state = {"i": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode("utf-8")
            prompts.append(json.loads(body)["messages"][0]["content"])
            content = replies[min(state["i"], len(replies) - 1)]
            state["i"] += 1
            resp = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(fetcher, "LLM_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}/v1")
        monkeypatch.setattr(fetcher, "GLOSSARY_CHUNK_WORDS", 3)  # 强制两块
        transcript = "Hot summer day.\nPower grid fails."  # 每行 3 词
        result = fetcher.generate_context_glossary(transcript, [], "test-key")
        assert result["hot"]["meaning"] == "热", "先到先得，重复词条不覆盖"
        assert result["grid"]["meaning"] == "电网"
        assert len(result) == 3
        assert "已给出过释义" in prompts[1] and "hot" in prompts[1], "第二块应携带已译词名单"
        assert "Power grid fails." in prompts[1] and "Hot summer day." not in prompts[1]
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
    baked = fetcher.bake_word_dict(episode)

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
    baked = fetcher.bake_word_dict(episode)
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
    baked = fetcher.bake_word_dict(episode)
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
        "title": "Is aircon the answer?",
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
    assert "aircon" in words and "answer" in words, "标题词也要发音（标题参与打字完词即播）"


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



# ---------------------------------------------------------------- 句子粒度翻译


def test_split_sentences():
    """按句末标点切分、多标点容忍、与前端 JS /(?<=[.!?])\s+/) 同构。"""
    text = "Hi Neil. Hi Georgie! Is this the end? No... maybe not."
    assert fetcher.split_sentences(text) == [
        "Hi Neil.",
        "Hi Georgie!",
        "Is this the end?",
        "No...",       # 省略号本身是句末标点（前后端同构，对齐不受影响）
        "maybe not.",
    ]
    # 引号包句（实测 261105 bug：?' 后跟换行曾漏切，导致整句翻译错位）
    quoted = "we'll be asking, 'Could vaccines cure cancer?'\nOK. Well, many."
    assert fetcher.split_sentences(quoted) == [
        "we'll be asking, 'Could vaccines cure cancer?'",
        "OK.",
        "Well, many.",
    ]
    # 括号/双引号收尾同样在切点之前（"No!" 是完整引语句，其后切分）
    assert fetcher.split_sentences('(Really?) Yes. He said "No!" twice.') == [
        "(Really?)",
        "Yes.",
        'He said "No!"',
        "twice.",
    ]
    assert fetcher.split_sentences("One sentence only") == ["One sentence only"]
    assert fetcher.split_sentences("") == []


def test_split_units_clause_level():
    """翻译单元 = 子句：句内逗号/冒号/分号切分（两侧 >=4 词才切），词数守恒。"""
    text = ("Hi Neil. In this episode of 6 Minute English, we'll be looking at some new research, "
            "and we'll be asking the question, 'Could vaccines cure cancer?'\nOK. Yes, of course.")
    units = fetcher.split_units(text)
    assert units == [
        "Hi Neil.",
        "In this episode of 6 Minute English,",       # 逗号后两侧均 >=4 词 → 切
        "we'll be looking at some new research,",
        "and we'll be asking the question,",
        "'Could vaccines cure cancer?'",
        "OK.",
        "Yes, of course.",   # 两侧不足 4 词，不切
    ], units
    # 词数守恒（对齐前提）
    assert sum(len(u.split()) for u in units) == len(text.split())


def test_generate_episode_translations(monkeypatch):
    """一次调用生成标题+逐句翻译；围栏 JSON 容错解析。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            captured["prompt"] = json.loads(self.rfile.read(length).decode("utf-8"))["messages"][0]["content"]
            content = '```json\n{"title": "空调是答案吗？", "sentences": ["你好，尼尔。", "你好，乔治。"]}\n```'
            resp = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(fetcher, "LLM_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}/v1")
        monkeypatch.setattr(fetcher, "LLM_MODEL", "glm-5.3-flash")
        result = fetcher.generate_episode_translations(
            "Is aircon the answer?", ["Hi Neil.", "Hi Georgie."], "k"
        )
        assert result == {"title": "空调是答案吗？", "sentences": ["你好，尼尔。", "你好，乔治。"]}
        assert "Is aircon the answer?" in captured["prompt"]
        assert "2 句" in captured["prompt"]
    finally:
        server.shutdown()
        server.server_close()


def test_bake_sentences_alignment():
    """切分与翻译对齐：多译丢弃、缺译留空。"""
    episode = {"transcript": "Hi Neil. Hi Georgie. Bye!"}
    translations = {"title": "t", "sentences": ["你好，尼尔。", "你好，乔治。", "多", "多2"]}
    sents = fetcher.bake_sentences(episode, translations)
    assert [s["text"] for s in sents] == ["Hi Neil.", "Hi Georgie.", "Bye!"]
    assert sents[0]["translation"] == "你好，尼尔。"
    assert sents[2]["translation"] == "多"  # 对齐截取


# ---------------------------------------------------------------- 全局词表挖掘


def test_global_glossary_save_load_aliases(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "GLOSSARY_FILE", tmp_path / "glossary.json")
    fetcher.save_global_glossary({"the grid": "电网", "we've": "我们已经"})
    saved = json.loads((tmp_path / "glossary.json").read_text(encoding="utf-8"))
    assert saved["the grid"] == "电网"
    assert saved["grid"] == "电网", "短语成分别名"
    assert saved["weve"] == "我们已经", "清洗别名（前端 cleanWord 键）"
    assert fetcher.load_global_glossary() == saved


def test_build_glossary_from_history(tmp_path, monkeypatch):
    """挖掘：只向 LLM 索要未入库的词（定向词表），无新词的块跳过，结果并入落盘。"""
    monkeypatch.setattr(fetcher, "GLOSSARY_FILE", tmp_path / "glossary.json")
    idx_html = (FIXTURES / "index.html").read_text(encoding="utf-8")
    ep_html = (FIXTURES / "episode-261029.html").read_text(encoding="utf-8")

    def fake_http_get(url):
        return idx_html if url == fetcher.INDEX_URL else ep_html

    prompts = []

    def fake_call(prompt, api_key, label):
        prompts.append(prompt)
        # 模拟模型：把该块点名的指定词全部返回
        import re as _re
        m = _re.search(r"指定词表（只输出这些词）：\n(.+)", prompt)
        wanted = m.group(1).split("、") if m else []
        return {w: {"meaning": f"译-{w}"} for w in wanted}

    monkeypatch.setattr(fetcher, "http_get", fake_http_get)
    monkeypatch.setattr(fetcher, "_glm_glossary_call", fake_call)

    rc = fetcher.build_glossary_from_history(1, "key")
    assert rc == 0
    data = json.loads((tmp_path / "glossary.json").read_text(encoding="utf-8"))
    assert "指定词表（只输出这些词）" in prompts[0]
    assert "hi" in data and data["hi"].startswith("译-"), "定向词条已入库"
    # 幂等：再挖一次，所有词已入库 → 全部块跳过、无 LLM 调用
    prompts.clear()
    rc = fetcher.build_glossary_from_history(1, "key")
    assert rc == 0 and not prompts, "无新词时不再调用 LLM"


# ---------------------------------------------------------------- rebake / TTS


def test_rebake_rebuilds_dict_keeps_glossary(tmp_path, monkeypatch):
    """rebake：重建词典（context+official 两层）、保留 context_glossary 与 transcript、
    不触网（独立模式在 parse_index 之前返回）。"""
    data_dir = tmp_path / "episodes"
    data_dir.mkdir()
    episode = {
        "id": "261001", "title": "T", "pub_date": "2026-10-01",
        "bbc_url": "https://example.com", "transcript": "summer heat wave",
        "vocabulary": [{"word": "heat wave", "definition": "a period of very hot weather"}],
        "context_glossary": {"heat wave": {"meaning": "热浪", "sentence": "s"}},
        "dict": {"onlyofficial": {"translation": "x", "source": "official"}},
    }
    (data_dir / "261001.json").write_text(json.dumps(episode, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(fetcher, "DATA_DIR", data_dir)
    monkeypatch.setattr(fetcher, "AUDIO_DIR", tmp_path / "audio")  # rebake 不碰音频

    rc = fetcher.build(days=30, no_llm=True, no_audio=True, rebake=True)
    assert rc == 0
    rebaked = json.loads((data_dir / "261001.json").read_text(encoding="utf-8"))
    assert rebaked["context_glossary"] == episode["context_glossary"], "语境词表必须保留"
    assert rebaked["transcript"] == episode["transcript"]
    # 语境层优先；未覆盖的词（summer/wave）不再有词典层兜底 → 不出现
    assert rebaked["dict"]["heat wave"]["source"] == "context"
    assert rebaked["dict"]["wave"]["source"] == "context", "短语成分别名继承语境层"
    assert "summer" not in rebaked["dict"], "无词典层：未覆盖词不出现"
    assert "onlyofficial" not in rebaked["dict"], "旧词典整体重建而非合并"


def test_build_refetches_corrupt_existing_episode(tmp_path, monkeypatch):
    """本地剧集文件半损坏（构建中断截断等）：必须当场重抓，而不是当作
    「已在架」跳过——否则 prune 把它删掉后该期要空窗到下次构建，
    且汇总日志把它误记为新增/跳过。重抓后二次构建恢复幂等跳过。"""
    from datetime import timedelta

    data_dir = tmp_path / "episodes"
    data_dir.mkdir()
    (data_dir / "261029.json").write_text('{"id": "261029", "transcript": "tru', encoding="utf-8")
    monkeypatch.setattr(fetcher, "DATA_DIR", data_dir)
    monkeypatch.setattr(fetcher, "AUDIO_DIR", tmp_path / "audio")

    ep_html = (FIXTURES / "episode-261029.html").read_text(encoding="utf-8")
    meta = {
        "id": "261029", "ep_num": "261029", "url": "https://www.bbc.co.uk/x",
        "title": "T", "pub_date": (date.today() - timedelta(days=1)).isoformat(),
    }
    monkeypatch.setattr(fetcher, "parse_index", lambda html: [meta])
    monkeypatch.setattr(fetcher, "http_get", lambda url: ep_html)

    rc = fetcher.build(days=30, no_llm=True, no_audio=True)
    assert rc == 0
    refetched = json.loads((data_dir / "261029.json").read_text(encoding="utf-8"))
    assert refetched["id"] == "261029" and len(refetched["transcript"].split()) > 800
    index = json.loads((data_dir / "index.json").read_text(encoding="utf-8"))
    assert [e["id"] for e in index] == ["261029"], "损坏文件重抓后在架，而非空窗到下次构建"

    # 幂等：文件恢复健康后，二次构建原样跳过（字节不变）
    before = (data_dir / "261029.json").read_text(encoding="utf-8")
    assert fetcher.build(days=30, no_llm=True, no_audio=True) == 0
    assert (data_dir / "261029.json").read_text(encoding="utf-8") == before


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
        "title": "Hi hot summer",        # 标题词同入音频词集
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


def test_prune_and_reindex_deletes_keyless_file(tmp_path):
    """JSON 合法但缺索引必需字段（id/title/bbc_url）的文件按损坏删除——
    而非等到索引构建时 KeyError 炸掉整个构建（main 只捕获 RequestException）。"""
    _write_episode(tmp_path, "261029", "2026-10-29")
    incomplete = {"id": "261030", "pub_date": "2026-10-30"}  # 缺 title/bbc_url
    (tmp_path / "261030.json").write_text(json.dumps(incomplete), encoding="utf-8")

    kept = fetcher.prune_and_reindex(tmp_path, date(2026, 10, 27))

    assert [d["id"] for d in kept] == ["261029"]
    assert not (tmp_path / "261030.json").exists()


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
