# TypeLearning（键学）

通过**键盘打字**学英语的静态站点：内容自动来自**最近 30 天的 BBC 6 Minute English**，
逐词打字跟读，输完一个单词（空格/标点结尾）即播放发音并显示释义——优先给出**官方词汇表
与 AI 语境释义**，普通词给出词典中文释义。学习进度保存在各自浏览器的 localStorage，
**多人使用互不干扰**，无需注册。

## 架构

```
GitHub Actions（每周日 05:00 北京时间）                    用户浏览器（GitHub Pages）
┌──────────────────────────────────────┐   git push      ┌─────────────────────────────┐
│ fetcher.py                           │  剧集 JSON      │ docs/（纯静态，无后端）      │
│  1. 抓 BBC 栏目页 → 最近 30 天剧集     │  （~60KB/周）   │  选剧集 → 对话式打字流       │
│  2. 解析对话回合/transcript/词汇表     │ ───────────►   │  （说话人徽章 + 全宽滚动）   │
│  3. GLM 生成全篇语境词表（可选）        │                │  完词即播 MP3 + 行内小字翻译 │
│  4. ECDICT 补全逐词中文释义            │  Pages 部署包   │  进度存 localStorage        │
│  5. edge-tts 生成单词发音 MP3（共享）   │ （站点+数据+    │  无 MP3 时回退浏览器 TTS     │
│  6. 清理过期剧集与无引用音频            │  音频，随 workflow ──────► Pages       │
└──────────────────────────────────────┘   直接部署）    └─────────────────────────────┘
```

所有重活（抓取、LLM、词典、TTS 发音）都在构建期完成；站点运行时**零后端、零额外网络请求**，
国内访问 GitHub Pages 即可用。

**体积设计**：单词发音 MP3（跨剧集共享、随窗口滚动清理，当前 ~11MB）**不进 git 仓库**——
存放在 Actions 缓存中滚动复用，随 Pages 部署包分发；仓库只保留剧集 JSON
（每周净增约 60KB，一年约 3MB）。历史提交中的音频 blob 无法自动移除（git 历史不可变），
但自本方案起不再增长。

## 开启站点（一次性）

1. 仓库 **Settings → Pages** → Build and deployment → Source 选 **GitHub Actions**
   （站点由 fetch workflow 部署，不再用 branch 部署）。
2. （可选，语境释义需要）**Settings → Secrets and variables → Actions → 「Secrets」标签页 → New repository secret**：
   - 名称 `GLM_API_KEY`，值为 [智谱开放平台](https://open.bigmodel.cn/) 的 API Key。
   - 注意是**仓库级 Secret**：不要放 Environment secret（workflow 未声明 `environment:`，读不到），
     更不要放 Variables（明文不加密，日志不打码）。
   - 默认调用 `glm-4-flash`（免费），如需换模型：在**「Variables」标签页**新建仓库变量 `LLM_MODEL`（明文配置放这里）。
   - 配置后手动 Run 一次 workflow，日志开头 `LLM：开` 即表示 key 已生效（`LLM：关` = 未配置或未读到）。
3. 手动触发一次 **Actions → Fetch 6 Minute English episodes → Run workflow** 验证。

不配置 GLM key 也能正常构建：释义退化为「官方词汇表 + ECDICT 词典」。

## 本地开发与验证

```bash
uv sync --dev
uv run pytest tests/ -q        # 解析器与滚动窗口测试（fixture 为真实页面快照）

# 本地构建数据（需能访问 BBC；如走代理，用标准环境变量 HTTPS_PROXY/HTTP_PROXY 指向你的代理）
HTTPS_PROXY=<你的代理地址> uv run python fetcher.py --no-llm

# 起静态服务浏览
python3 -m http.server -d docs 8000   # 打开 http://127.0.0.1:8000
```

浏览器端到端测试（可选，需要 Playwright + Chromium；国内下载二进制用 npmmirror 镜像）：

```bash
pip install playwright
PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright/ python -m playwright install chromium
python3 -m http.server -d docs 8901 &
python tests/e2e_site.py     # 24 项 GUI 检查 + 滚动窗口兼容专测，截图见 tests/e2e-screenshots/
                             # 前置：本地需先完整构建（含音频，勿用 --no-audio）——
                             # 其中有断言依赖 docs/data/audio/hi.mp3 在架
```

`fetcher.py [--days N] [--no-llm]`：抓取最近 N 天剧集、烘焙 `docs/data/episodes/`
（`index.json` + 每期一个 JSON），并清理过期文件。产物是确定性的——没有新剧集时
不产生 git diff，Actions 不会空提交。

## 数据与隐私设计

- **滚动 30 天窗口**：站点只保留最近 30 天的剧集（BBC 每周四更新一期，在架约 4–5 期），
  过期剧集的数据文件由构建自动删除；Actions 每周日凌晨跑一次（也支持手动触发）。
- **本地存储兼容**：进度按剧集存于 localStorage（`typelearning:progress:{id}`，带版本号）。
  启动时会校验「继续上次」的剧集是否仍在架：已下线则提示并自动清理对应进度（GC），
  存储不会无限膨胀；剧集列表会标注「剩 N 天」与已完成 ✓。
- **多用户隔离**：没有任何服务器状态，不同浏览器/设备各自独立进度。

## 目录说明

```
fetcher.py                    # 构建脚本（抓取/解析/GLM/ECDICT/清理，默认 30 天窗口）
docs/                         # 站点（index.html + script.js + assets/pico.min.css 自托管样式）
docs/data/episodes/           # 构建产物：index.json + 每期剧集 JSON（含烘焙词典，提交进仓库）
docs/data/audio/              # 构建产物：单词发音 MP3（gitignore；Actions 缓存 + Pages 部署包分发）
.github/workflows/            # 每周构建 workflow（周日北京 05:00 + 手动触发，含 Pages 部署）
tests/                        # pytest（fixtures 为 BBC 真实页面快照）
ecdict.sqlite                 # 本地离线词典（gitignore；Actions 中按 Release 缓存下载）
```

## 注意事项

- `docs/data/` 中的 transcript 与词汇表来自 BBC 公开页面，本仓库公开转发仅作个人学习用途；
  若将来需要扩大使用范围，建议把数据拆到 private 仓库（代码已隔离在 `docs/data/` 单一目录，
  迁移时只需改 workflow 的 push 目标与 Pages 来源）。
- BBC 页面改版会导致解析失败（测试有真实页面快照兜底，Actions 日志会显示具体原因），
  修复时对照 `tests/fixtures/` 更新解析器即可。
- 发音优先播放构建期烘焙的 MP3（本地文件、零延迟，不依赖浏览器语音）；仅当某词音频
  生成失败或缺失时回退浏览器语音合成（Chrome / Edge / Safari 桌面端均有离线英文语音）。
  音频随剧集滚动清理（按在架剧集的词集引用计数）。
- 音频缓存放于 Actions cache（滚动 key），被 GC 回收后下次构建会自动全量重建（约 10 分钟），
  不影响站点可用性。
