# english-typing-reader（英语打字阅读练习）

一个英语打字阅读练习 Web 应用：上传英文文本后逐词打字跟读，输入正确的单词即自动前进，同时提供单词发音和中文翻译，并自动保存学习进度。

## 技术栈

- **后端**：Python 3.11+ / FastAPI / uvicorn
- **前端**：原生 HTML + CSS + JavaScript（无框架）
- **依赖服务**：
  - **发音**：[edge-tts](https://pypi.org/project/edge-tts/)（微软 Edge TTS，国内可直连，首选）→ [gTTS](https://pypi.org/project/gTTS/)（Google TTS，备用）；生成的音频按单词缓存为 MP3
  - **翻译**：ECDICT 本地离线词典（77 万词条，查词零网络、毫秒级，首选）→ 有道词典接口（国内快，免 key）→ [googletrans](https://pypi.org/project/googletrans/)（Google 翻译，兜底）

## 功能

- 上传 `.txt` / `.md` 英文文本，或从已上传列表中选择
- 逐词打字练习：当前单词高亮显示，已完成的标绿；输入错误时输入框变红提示
- 单词输入正确后自动播放发音，并在侧边栏显示中文翻译
- 学习进度自动保存（每完成一个单词及关闭页面时），下次进入可从上次位置继续
- 长文本窗口化渲染（当前词前后各 30 词），并自动滚动跟随
- 响应式布局，适配手机屏幕

## 快速开始

使用 [uv](https://docs.astral.sh/uv/) 运行：

```bash
uv run uvicorn main:app --reload
```

或使用 pip：

```bash
pip install fastapi uvicorn gtts googletrans==4.0.0rc1 aiofiles python-multipart requests
uvicorn main:app --reload
```

然后浏览器打开 <http://127.0.0.1:8000>。

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 主页面 |
| POST | `/api/upload_text/` | 上传文本文件 |
| GET | `/api/list_texts/` | 列出已上传文本 |
| GET | `/api/get_text/{filename}` | 获取文本内容 |
| POST | `/api/save_progress/` | 保存学习进度 |
| GET | `/api/load_progress/` | 加载学习进度 |
| GET | `/api/pronounce/{word}` | 单词发音（MP3 音频流） |
| GET | `/api/translate/{word}` | 单词中文翻译 |

## 目录说明

```
main.py           # FastAPI 后端（文本管理、进度、发音、翻译）
static/           # 前端页面与脚本
ecdict.sqlite     # 本地离线英汉词典（需单独下载，已忽略）
texts/            # 上传的文本存放目录（运行时生成，已忽略）
audio_cache/      # 发音 MP3 缓存（运行时生成，已忽略）
progress.json     # 学习进度文件（运行时生成，已忽略）
translation_cache.json  # 在线翻译结果缓存（运行时生成，已忽略）
```

## 注意事项

- 词典文件 `ecdict.sqlite`（132MB）不随仓库分发，放在项目根目录即可自动加载；缺失时翻译自动回退到在线方式。可从 [ECDICT](https://github.com/skywind3000/ECDICT) releases 获取。
- 发音首选微软 edge-tts（走国内可达的 Bing 节点），首次生成后缓存到 `audio_cache/`，重复词毫秒级。
- 有道词典接口为免 key 的网页接口，googletrans 为非官方库——它们只作为本地词典未命中时的回退，失效不影响主流程。
- 为避免国内访问 Google 慢导致卡顿，所有在线请求都有超时保护，且发音/翻译在前端并行请求并预取下一词。
