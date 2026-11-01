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


def test_bake_word_dict_priority_and_alias():
    episode = {
        "transcript": "The grid supplies power. Life-saving machines inhabit hospitals.",
        "vocabulary": [{"word": "the grid", "definition": "the electricity network"}],
        "context_glossary": {
            "inhabit": {"meaning": "（大量人）居住在", "sentence": "inhabit hospitals"}
        },
    }
    baked = fetcher.bake_word_dict(episode, None)

    # 短语本体与成分别名（前端按空白分词，只能查到 grid）
    assert baked["the grid"]["source"] == "official"
    assert baked["grid"]["source"] == "official"
    assert baked["grid"]["phrase"] == "the grid"
    # 连字符别名：前端 cleanWord("life-saving") == "lifesaving"
    assert "lifesaving" not in baked, "无来源覆盖的词不应凭空出现"
    # 语境层
    assert baked["inhabit"]["source"] == "context"
    assert baked["inhabit"]["context"] == "（大量人）居住在"
    assert baked["inhabit"]["sentence"] == "inhabit hospitals"
    assert baked["inhabit"]["translation"] == "（大量人）居住在"


def test_bake_word_dict_official_overrides_context():
    episode = {
        "transcript": "shading blocks the sun",
        "vocabulary": [{"word": "shading", "definition": "official def"}],
        "context_glossary": {"shading": {"meaning": "ctx meaning", "sentence": "s"}},
    }
    baked = fetcher.bake_word_dict(episode, None)
    assert baked["shading"]["source"] == "official"
    assert baked["shading"]["translation"] == "official def"


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
