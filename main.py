import os
import re
import json
import asyncio
import sqlite3
import uuid
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from gtts import gTTS
import edge_tts
import aiofiles
import requests # 仍然需要requests，因为googletrans可能内部用到，或者未来扩展

# 尝试导入 googletrans
try:
    from googletrans import Translator
    translator = Translator()
    print("googletrans 库已成功导入。")
except ImportError:
    translator = None
    print("警告：未安装 googletrans 库。中文翻译功能将无法使用。请运行 'pip install googletrans==4.0.0rc1'")
except Exception as e:
    translator = None
    print(f"警告：导入 googletrans 库时发生错误: {e}。中文翻译功能将无法使用。")


app = FastAPI()

# 静态文件目录
STATIC_DIR = Path("static")
STATIC_DIR.mkdir(exist_ok=True)

# 文本文件存储目录
TEXTS_DIR = Path("texts")
TEXTS_DIR.mkdir(exist_ok=True)

# 音频缓存目录
AUDIO_CACHE_DIR = Path("audio_cache")
AUDIO_CACHE_DIR.mkdir(exist_ok=True)
# 清理上次运行遗留的临时音频片段（超时后被取消的生成任务可能迟到落盘）
for _stale in AUDIO_CACHE_DIR.glob("*.part"):
    _stale.unlink(missing_ok=True)

# 进度文件路径
PROGRESS_FILE = Path("progress.json")

# 翻译缓存文件路径（googletrans 每次都实时请求 Google，很慢且不稳定，必须缓存）
TRANSLATION_CACHE_FILE = Path("translation_cache.json")

# 访问 Google（gTTS/googletrans）的超时秒数：网络不稳时快速失败，避免请求无限挂起
EXTERNAL_REQUEST_TIMEOUT = 8.0

# 启动时加载磁盘上的翻译缓存
translation_cache: dict = {}
if TRANSLATION_CACHE_FILE.exists():
    try:
        with open(TRANSLATION_CACHE_FILE, "r", encoding="utf-8") as f:
            translation_cache = json.load(f)
        print(f"已加载翻译缓存，共 {len(translation_cache)} 个单词。")
    except Exception as e:
        print(f"警告：加载翻译缓存失败: {e}")


def _save_translation_cache():
    try:
        with open(TRANSLATION_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(translation_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"警告：写入翻译缓存失败: {e}")


# 本地离线词典（ECDICT 英汉词典）：查词零网络、毫秒级，避免依赖 Google 翻译
DICT_DB_FILE = Path("ecdict.sqlite")

dict_conn: Optional[sqlite3.Connection] = None
if DICT_DB_FILE.exists():
    try:
        dict_conn = sqlite3.connect(str(DICT_DB_FILE), check_same_thread=False)
        dict_conn.execute("SELECT translation FROM entries WHERE word = 'test'").fetchone()
        print("ECDICT 本地词典已加载，单词翻译将优先走本地查词。")
    except Exception as e:
        dict_conn = None
        print(f"警告：加载 ECDICT 词典失败: {e}，翻译将回退到在线方式。")
else:
    print("提示：未找到 ecdict.sqlite，翻译将使用在线方式。")


def lookup_local_dict(word: str) -> Optional[str]:
    """查本地词典，返回中文释义；未命中返回 None"""
    if dict_conn is None:
        return None
    try:
        row = dict_conn.execute(
            "SELECT translation FROM entries WHERE word = ? AND translation != ''", (word,)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row and row[0]:
        # 释义按换行分隔多条（含字面 \n），合并为一行便于前端展示
        merged = row[0].replace('\\n', '；').replace('\r', '').replace('\n', '；')
        return re.sub(r'；+', '；', merged).strip('； ')
    return None


def translate_via_youdao(word: str) -> Optional[str]:
    """有道词典查词（国内访问快、无需 API key）；失败返回 None"""
    try:
        resp = requests.get(
            "https://dict.youdao.com/jsonapi_s",
            params={"doctype": "json", "jsonversion": "4", "q": word},
            timeout=EXTERNAL_REQUEST_TIMEOUT,
        )
        data = resp.json()
        # 优先取英汉词典释义（带词性），如 "n. 象，大象"
        ec_word = (data.get("ec") or {}).get("word") or {}
        lines = [
            f"{t.get('pos', '')} {t.get('tran', '')}".strip()
            for t in (ec_word.get("trs") or []) if t.get("tran")
        ]
        if lines:
            return "；".join(lines)
        # 回退：网络释义第一条
        web = (data.get("web_trans") or {}).get("web-translation") or []
        if web:
            trans = web[0].get("trans") or []
            if trans and trans[0].get("value"):
                return str(trans[0]["value"]).strip()
    except Exception as e:
        print(f"youdao 查询失败: {e}")
    return None


# 发音服务：edge-tts 走微软国内可达节点，作为首选；gTTS 作为备用
TTS_VOICE = "en-US-AriaNeural"

# 硬编码的单词解释 (这个现在彻底不需要了，留空即可)
WORD_DEFINITIONS = {}


# 定义进度模型
class Progress(BaseModel):
    current_text_filename: str
    current_word_index: int

# 挂载静态文件目录
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/", response_class=HTMLResponse)
async def read_root():
    """提供主页面HTML"""
    return FileResponse(STATIC_DIR / "index.html")

@app.post("/api/upload_text/")
async def upload_text(file: UploadFile = File(...)):
    """上传文本文件"""
    # 只保留文件名部分并校验后缀：multipart 文件名由客户端控制，
    # 不清洗会导致路径穿越（../ 或绝对路径）写任意文件
    safe_name = Path(file.filename or "").name
    if not safe_name or Path(safe_name).suffix.lower() not in ('.txt', '.md'):
        raise HTTPException(status_code=400, detail="仅支持上传 .txt / .md 文本文件")
    file_location = TEXTS_DIR / safe_name
    try:
        async with aiofiles.open(file_location, "wb") as buffer:
            while content := await file.read(1024):
                await buffer.write(content)
        return {"message": f"文件 '{safe_name}' 上传成功", "filename": safe_name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"文件上传失败: {e}")

@app.get("/api/list_texts/")
async def list_texts():
    """获取已上传的文本文件列表"""
    files = [f.name for f in TEXTS_DIR.iterdir() if f.is_file() and f.suffix in ['.txt', '.md']]
    return files

@app.get("/api/get_text/{filename}")
async def get_text(filename: str):
    """获取指定文本文件的内容"""
    file_path = TEXTS_DIR / filename
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="文件未找到")
    try:
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            content = await f.read()
        return {"filename": filename, "content": content}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取文件失败: {e}")

@app.post("/api/save_progress/")
async def save_progress(progress: Progress):
    """保存用户学习进度"""
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(progress.model_dump(), f, ensure_ascii=False, indent=4)
        return {"message": "进度保存成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存进度失败: {e}")

@app.get("/api/load_progress/")
async def load_progress():
    """加载用户学习进度"""
    print("后端调试：尝试加载进度文件...") # 新增日志
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                progress_data = json.load(f)
                print(f"后端调试：从 {PROGRESS_FILE} 读取到的原始数据: {progress_data}") # 新增日志
                
                # 再次检查读取到的数据类型，确保 filename 是字符串
                filename_from_file = progress_data.get("current_text_filename")
                if not isinstance(filename_from_file, str):
                    print(f"后端调试：current_text_filename 类型异常，为: {type(filename_from_file)}") # 新增日志
                    # 如果不是字符串，将其转换为None或空字符串，避免前端出错
                    progress_data["current_text_filename"] = None # 或者 ""
                
                return progress_data
        except json.JSONDecodeError as e:
            print(f"后端调试：进度文件 {PROGRESS_FILE} JSON解码失败: {e}") # 新增日志
            return {"current_text_filename": None, "current_word_index": 0} # 确保返回结构一致
        except Exception as e:
            print(f"后端调试：加载进度失败，发生未知错误: {e}") # 新增日志
            raise HTTPException(status_code=500, detail=f"加载进度失败: {e}")
    print(f"后端调试：进度文件 {PROGRESS_FILE} 不存在。") # 新增日志
    return {"current_text_filename": None, "current_word_index": 0} # 如果没有进度文件，返回默认值

@app.get("/api/pronounce/{word}")
async def pronounce_word(word: str):
    """
    为指定单词生成并提供发音。
    首先检查本地缓存，如果不存在则使用 gTTS 生成并缓存。
    """
    # 彻底清理单词，只保留字母和数字，用于发音和翻译
    cleaned_word = re.sub(r'[^a-zA-Z0-9]', '', word).lower()
    if not cleaned_word:
        raise HTTPException(status_code=400, detail="单词为空或无效")

    audio_file_path = AUDIO_CACHE_DIR / f"{cleaned_word}.mp3"

    if not audio_file_path.exists():
        # 先写临时文件再原子替换：任一环节失败都不残留空/半截 MP3 被当作有效缓存。
        # 临时名必须每请求唯一：同词并发请求共用一个 .part 会互相截断写坏缓存
        tmp_path = AUDIO_CACHE_DIR / f"{cleaned_word}.{os.getpid()}.{uuid.uuid4().hex}.part"
        try:
            try:
                # 首选 edge-tts：微软服务国内直连、速度快，原生异步不阻塞事件循环
                communicate = edge_tts.Communicate(cleaned_word, TTS_VOICE)
                await asyncio.wait_for(communicate.save(str(tmp_path)), timeout=EXTERNAL_REQUEST_TIMEOUT)
            except Exception as e:
                print(f"edge-tts 生成发音失败: {e}，回退 gTTS...")
                # 备用 gTTS：放线程池，并给 gTTS 自身传超时，避免 wait_for 取消后留下无限等待的孤儿线程
                def generate_via_gtts():
                    tts = gTTS(text=cleaned_word, lang='en', slow=False, timeout=EXTERNAL_REQUEST_TIMEOUT)
                    tts.save(tmp_path)
                await asyncio.wait_for(asyncio.to_thread(generate_via_gtts), timeout=EXTERNAL_REQUEST_TIMEOUT)

            if not tmp_path.exists() or tmp_path.stat().st_size == 0:
                raise RuntimeError("生成的音频内容为空")
            os.replace(tmp_path, audio_file_path)
        except asyncio.TimeoutError:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=504, detail="生成发音超时（TTS 服务不可达），请稍后重试")
        except Exception as e:
            tmp_path.unlink(missing_ok=True)
            print(f"生成发音失败: {e}")
            raise HTTPException(status_code=500, detail=f"生成发音失败: {e}")

    def iterfile():
        with open(audio_file_path, mode="rb") as file_like:
            yield from file_like

    return StreamingResponse(iterfile(), media_type="audio/mpeg")


@app.get("/api/translate/{word}")
async def get_word_translation(word: str):
    """
    获取单词的中文翻译：优先本地 ECDICT 离线词典（毫秒级），
    未命中再走 googletrans 在线翻译（带缓存与超时）。
    """
    cleaned_word = re.sub(r'[^a-zA-Z0-9]', '', word).lower() # 清理单词，只保留字母和数字
    if not cleaned_word:
        # 确保返回的结构与前端期望的 { definition, translation } 匹配
        # 尽管我们只用translation，但为了兼容性，可以返回空definition
        return JSONResponse(content={"translation": "无效单词", "definition": ""})

    # 本地离线词典优先：零网络、毫秒级
    local_result = lookup_local_dict(cleaned_word)
    if local_result:
        return JSONResponse(content={"translation": local_result, "definition": ""})

    # 在线翻译缓存（有道/googletrans 的历史结果）
    cached = translation_cache.get(cleaned_word)
    if cached:
        return JSONResponse(content={"translation": cached, "definition": ""})

    # 有道词典（国内访问快）→ googletrans 兜底；两者都是同步库，放线程池 + 超时
    translated_text = None
    try:
        translated_text = await asyncio.wait_for(
            asyncio.to_thread(translate_via_youdao, cleaned_word), timeout=EXTERNAL_REQUEST_TIMEOUT
        )
    except asyncio.TimeoutError:
        print("youdao 翻译超时")

    if not translated_text and translator:
        try:
            translated_text = await asyncio.wait_for(
                asyncio.to_thread(lambda: translator.translate(cleaned_word, dest='zh-cn').text),
                timeout=EXTERNAL_REQUEST_TIMEOUT,
            )
        except asyncio.TimeoutError:
            return JSONResponse(content={"translation": "（翻译超时，请稍后重试）", "definition": ""})
        except Exception as e:
            print(f"使用 googletrans 翻译失败: {e}")

    if translated_text:
        translation_cache[cleaned_word] = translated_text
        _save_translation_cache()
        return JSONResponse(content={"translation": translated_text, "definition": ""})

    return JSONResponse(content={"translation": "（翻译服务暂不可用，请稍后重试）", "definition": ""})

