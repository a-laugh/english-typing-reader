# 小小打字员（little-typist）

一个英语打字阅读练习 Web 应用：上传英文文本后逐词打字跟读，输入正确的单词即自动前进，同时提供单词发音和中文翻译，并自动保存学习进度。

## 技术栈

- **后端**：Python 3.11+ / FastAPI / uvicorn
- **前端**：原生 HTML + CSS + JavaScript（无框架）
- **依赖服务**：
  - [gTTS](https://pypi.org/project/gTTS/) — 生成单词发音（英语），按单词缓存为 MP3
  - [googletrans](https://pypi.org/project/googletrans/) — 单词中文翻译（可选，未安装时翻译功能降级）

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
texts/            # 上传的文本存放目录（运行时生成，已忽略）
audio_cache/      # 发音 MP3 缓存（运行时生成，已忽略）
progress.json     # 学习进度文件（运行时生成，已忽略）
```

## 注意事项

- 发音与翻译依赖 Google 的在线服务，需要能访问外网；发音首次生成后会缓存到本地 `audio_cache/`。
- `googletrans` 为非官方库，若翻译接口失效，侧边栏会显示相应错误提示，但不影响打字练习主流程。
