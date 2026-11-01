#!/usr/bin/env python3
"""静态站点端到端测试（Playwright 无头浏览器，GUI 黑盒）。

前置：docs/data 已构建（含 261029 与 261022 两期），本地服务已启动：
    python3 -m http.server -d docs 8901
运行：
    uv run --with playwright python tests/e2e_site.py

滚动窗口专测会临时改写 docs/data/episodes/（模拟剧集过期）并随后恢复。
截图保存到 tests/e2e-screenshots/。
"""

import json
import shutil
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8901"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data" / "episodes"
SHOTS = Path(__file__).resolve().parent / "e2e-screenshots"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" —— {detail}" if detail and not cond else ""))


def main() -> int:
    SHOTS.mkdir(exist_ok=True)
    console_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(str(e)))
        seen_requests: list[str] = []
        page.on("request", lambda r: seen_requests.append(r.url))

        print("== T1 首页加载与剧集列表 ==")
        page.goto(BASE)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_selector("#episode-selector option", state="attached", timeout=5000)
        options = page.eval_on_selector_all("#episode-selector option", "els => els.map(e => ({v: e.value, t: e.textContent}))")
        check("剧集下拉含多期真实节目（≥2）", sum(1 for o in options if o["v"]) >= 2, str(options))
        check("列表标注发布日期", any("10/29" in o["t"] for o in options), str(options))
        page.screenshot(path=str(SHOTS / "t1_home.png"))

        print("== T2 加载节目与打字主流程（选择即加载） ==")
        page.select_option("#episode-selector", "261029")
        page.wait_for_selector(".current-word", timeout=5000)
        page.wait_for_timeout(500)
        audio_reqs = {u for u in seen_requests if "/data/audio/" in u}
        check("加载节目即窗口化预取音频（≥5 词）", len(audio_reqs) >= 5, f"{len(audio_reqs)} 个: {sorted(audio_reqs)[:3]}")
        first_word = page.text_content(".current-word")
        check("transcript 已渲染且首词高亮", first_word == "Hi", f"首词={first_word!r}")
        badges = page.eval_on_selector_all(".speaker-badge", "els => els.map(e => e.textContent)")
        check("对话回合带说话人徽章", len(badges) >= 3 and badges[0] == "Georgie", str(badges[:4]))
        meta = page.text_content("#episode-meta")
        check("meta 栏含日期与剩余天数", "2026-10-29" in meta and "剩" in meta, meta)
        hint = page.text_content("#hint-progress")
        check("提示行显示进度", "0/" in hint, hint)
        check("BBC 原文链接存在", page.eval_on_selector("#episode-meta a", "a => a.href").endswith("ep-261029"))
        page.screenshot(path=str(SHOTS / "t2_loaded.png"))

        # 正确输入第一个词 "Hi"（以空格结束）
        page.fill("#typing-input", "Hi ")
        page.wait_for_function(
            "() => document.querySelectorAll('.word-correct').length === 1", timeout=3000
        )
        check("正确完词后前进（1 词标绿）", True)
        check("完词播放的是构建期烘焙音频", any(u.endswith("data/audio/hi.mp3") for u in seen_requests),
              str([u for u in seen_requests if "audio" in u][:3]))
        check("烘焙音频文件在架", (ROOT / "docs" / "data" / "audio" / "hi.mp3").is_file())
        gloss = page.text_content("#dialog-display .gloss")
        check("行内翻译紧跟完成的词", gloss and any(k in gloss for k in ("你好", "喂", "嗨", "问候")), gloss)

        # 正确输入第二个词 "Neil."（词尾标点，无需输入标点）
        page.fill("#typing-input", "neil ")
        page.wait_for_function(
            "() => document.querySelectorAll('.word-correct').length === 2", timeout=3000
        )
        check("第二词完成后累计 2 词标绿", True)
        gloss_count = page.eval_on_selector_all("#dialog-display .gloss", "els => els.length")
        check("行内翻译只跟随最新完成的词（唯一）", gloss_count == 1, f"gloss 数={gloss_count}")

        # 错误输入 → 红框
        page.fill("#typing-input", "xyz")
        page.wait_for_selector("#typing-input.incorrect-input-border", timeout=3000)
        check("输错前缀时输入框变红", True)
        page.screenshot(path=str(SHOTS / "t3_typing.png"))

        print("== T3 刷新后自动续读（localStorage 进度） ==")
        page.reload()
        page.wait_for_selector(".word-correct", timeout=5000)
        correct_count = page.eval_on_selector_all(".word-correct", "els => els.length")
        check("刷新后从进度续读（仍有 2 词标绿）", correct_count == 2, f"标绿={correct_count}")
        check("自动选中上次的剧集", page.eval_on_selector("#episode-selector", "el => el.value") == "261029")
        page.screenshot(path=str(SHOTS / "t4_resume.png"))

        print("== T4 换剧集互不干扰 ==")
        page.select_option("#episode-selector", "261022")
        page.wait_for_function(
            "() => document.querySelectorAll('.word-correct').length === 0", timeout=5000
        )
        check("切换剧集从头开始（无标绿词）", True)
        meta2 = page.text_content("#episode-meta")
        check("meta 栏切换为新剧集日期", "2026-10-22" in meta2, meta2)
        # 完成一个词，制造 261022 的进度，供 T5 模拟过期
        page.fill("#typing-input", "hi ")
        page.wait_for_selector(".word-correct", timeout=3000)
        check("第二剧集可正常完词", True)

        print("== T5 滚动窗口：剧集过期下线 ==")
        # 模拟次日构建：261022 超过 7 天窗口被清理（fetcher 的 prune 行为）
        backup = SHOTS / "data-backup"
        if backup.exists():
            shutil.rmtree(backup)
        backup.mkdir()
        for f in DATA.glob("*.json"):
            shutil.copy2(f, backup / f.name)
        try:
            (DATA / "261022.json").unlink()
            index = [e for e in json.loads((DATA / "index.json").read_text(encoding="utf-8")) if e["id"] != "261022"]
            # 重写后显式把 mtime 拨到未来：否则若此前的 mtime 被拨到过未来
            # （其他测试的 utime 补偿），秒级粒度下 revalidation 会误判 304 拿到旧列表
            import os as _os
            import time as _time
            (DATA / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            _bumped = _time.time() + 2
            _os.utime(DATA / "index.json", (_bumped, _bumped))

            page.reload()
            page.wait_for_selector("#episode-selector option", state="attached", timeout=5000)
            time.sleep(0.5)
            notice = page.text_content("#notice")
            check("过期后提示『上次的节目已下线』", notice and "已下线" in notice, notice)
            options2 = page.eval_on_selector_all("#episode-selector option", "els => els.map(e => e.value)")
            check("列表只剩在架剧集", "261022" not in options2 and "261029" in options2, str(options2))
            gc_ok = page.evaluate("() => localStorage.getItem('typelearning:progress:261022') === null")
            check("已下线剧集的进度被 GC", gc_ok)
            kept_ok = page.evaluate("() => localStorage.getItem('typelearning:progress:261029') !== null")
            check("在架剧集进度保留", kept_ok)
            page.screenshot(path=str(SHOTS / "t5_expired.png"))
        finally:
            import os
            import time as _time
            # mtime 只有秒级粒度，且必须严格大于 T5 剪枝版写入时的补偿时间（+2s），
            # 否则服务器对 If-Modified-Since 会误判 304，浏览器继续用剪枝后的缓存
            bumped = _time.time() + 10
            for f in backup.glob("*.json"):
                # copyfile（不带元数据）：恢复后 mtime 为当下。
                # 若用 copy2 保留旧 mtime，浏览器带 If-Modified-Since 校验会拿到 304，
                # 继续用剪枝后的缓存列表（真实构建每次落盘都是新 mtime，无此问题）
                shutil.copyfile(f, DATA / f.name)
                os.utime(DATA / f.name, (bumped, bumped))
            shutil.rmtree(backup)

        print("== T6 数据恢复后一切正常 ==")
        page.reload()
        page.wait_for_selector("#episode-selector option", state="attached", timeout=5000)
        options3 = page.eval_on_selector_all("#episode-selector option", "els => els.map(e => e.value)")
        check("恢复数据后两期重新在架", "261022" in options3 and "261029" in options3, str(options3))
        # 261022 的进度已被 GC，手动选择 261029 应自动续读其保留进度（2 词标绿）
        page.select_option("#episode-selector", "261029")
        page.wait_for_function(
            "() => document.querySelectorAll('.word-correct').length === 2", timeout=5000
        )
        check("手动选择 261029 自动续读到之前的进度", True)

        browser.close()

    print(f"\n结果：{len(PASS)} 通过，{len(FAIL)} 失败")
    if console_errors:
        print("页面 console 错误：")
        for err in console_errors[:10]:
            print("  -", err[:200])
    else:
        print("页面 console 无错误")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
