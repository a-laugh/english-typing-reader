import os
import re
import json
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from gtts import gTTS
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

# 进度文件路径
PROGRESS_FILE = Path("progress.json")

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
    file_location = TEXTS_DIR / file.filename
    try:
        async with aiofiles.open(file_location, "wb") as buffer:
            while content := await file.read(1024):
                await buffer.write(content)
        return {"message": f"文件 '{file.filename}' 上传成功", "filename": file.filename}
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
        try:
            tts = gTTS(text=cleaned_word, lang='en', slow=False)
            tts.save(audio_file_path)
        except Exception as e:
            print(f"gTTS 生成发音失败: {e}")
            raise HTTPException(status_code=500, detail=f"生成发音失败: {e}")

    def iterfile():
        with open(audio_file_path, mode="rb") as file_like:
            yield from file_like

    return StreamingResponse(iterfile(), media_type="audio/mpeg")


@app.get("/api/translate/{word}")
async def get_word_translation(word: str):
    """
    通过 googletrans 获取单词的中文翻译。
    """
    cleaned_word = re.sub(r'[^a-zA-Z0-9]', '', word).lower() # 清理单词，只保留字母和数字
    if not cleaned_word:
        # 确保返回的结构与前端期望的 { definition, translation } 匹配
        # 尽管我们只用translation，但为了兼容性，可以返回空definition
        return JSONResponse(content={"translation": "无效单词", "definition": ""})

    if translator:
        try:
            # 假设源语言是英文，目标语言是中文
            translated_text = translator.translate(cleaned_word, dest='zh-cn').text
            # 返回时，definition字段为空字符串，确保前端只显示translation
            return JSONResponse(content={"translation": translated_text, "definition": ""})
        except Exception as e:
            print(f"使用 googletrans 翻译失败: {e}")
            return JSONResponse(content={"translation": f"（翻译服务出错：{e}）", "definition": ""})
    else:
        return JSONResponse(content={"translation": "（后端翻译功能未启用，请检查 googletrans 安装）", "definition": ""})

