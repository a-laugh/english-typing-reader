// static/script.js
// 全局变量
let originalText = ""; // 存储当前加载的原文的完整内容
let originalWords = []; // 存储所有单词的数组
let currentWordIndex = 0; // 当前正在输入的单词的索引
let typedInput = ""; // 用户在输入框中输入的内容
let currentWordToPronounce = ""; // 存储当前要发音的单词
let currentLoadedFilename = ""; // 记录当前加载的文件名，用于保存进度

// DOM 元素引用
const originalTextDisplay = document.getElementById('original-text-display');
const typingInput = document.getElementById('typing-input');
const resultWordSpan = document.querySelector('#result-display .result-word');
const resultDefinitionSpan = document.querySelector('#result-display .result-definition');
const pronounceButton = document.getElementById('pronounce-button');
const audioPlayer = new Audio();

// 文件上传相关 DOM 元素
const fileUploadInput = document.getElementById('file-upload');
const uploadButton = document.getElementById('upload-btn');
const textSelector = document.getElementById('text-selector');
const loadTextButton = document.getElementById('load-text-btn');


// --- 新增配置：控制显示文本的行数 ---
const DISPLAY_WORDS_AROUND_CURRENT = 30; // 在当前单词前后各显示多少个单词，构成一个“窗口”

// --- 辅助函数：清除单词的标点符号 ---
function cleanWord(word) {
    // 移除单词末尾的标点符号，例如 "dog." -> "dog"
    // 但保留单词内部的连字符等，如果需要的话。这里只处理末尾
    // 确保处理后只剩下字母和数字
    return word.toLowerCase().replace(/[^a-z0-9]/g, ''); // 修正为只保留字母和数字
}

// --- 渲染原文显示 (只在需要时滚动) ---
function renderOriginalText() {
    const startIndex = Math.max(0, currentWordIndex - DISPLAY_WORDS_AROUND_CURRENT);
    const endIndex = Math.min(originalWords.length, currentWordIndex + DISPLAY_WORDS_AROUND_CURRENT);

    const wordsToRender = [];
    for (let i = startIndex; i < endIndex; i++) {
        const word = originalWords[i];
        let spanClass = '';
        if (i < currentWordIndex) {
            spanClass = 'word-correct'; 
        } else if (i === currentWordIndex) {
            spanClass = 'current-word'; 
        }
        wordsToRender.push(`<span class="${spanClass}">${word}</span>`);
    }

    originalTextDisplay.innerHTML = wordsToRender.join(' ');

    const currentWordSpan = originalTextDisplay.querySelector('.current-word');
    if (currentWordSpan) {
        // --- 核心修改：仅当当前单词不在视图内时才滚动 ---
        const originalTextRect = originalTextDisplay.getBoundingClientRect();
        const currentWordRect = currentWordSpan.getBoundingClientRect();

        // 检查当前单词是否完全在视图内
        const isPartiallyVisible = currentWordRect.top < originalTextRect.bottom &&
                                   currentWordRect.bottom > originalTextRect.top;

        if (!isPartiallyVisible || currentWordRect.bottom > originalTextRect.bottom || currentWordRect.top < originalTextRect.top) {
            currentWordSpan.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }
    }
}


// --- 获取单词翻译 (现在只获取中文翻译) ---
async function getWordTranslationAndDefinition(word) {
    try {
        const cleanedWordForApi = cleanWord(word);
        if (!cleanedWordForApi) {
            return { definition: "", translation: "无效单词" }; // 返回空定义，确保结构
        }

        // 调用后端API，后端现在只返回翻译
        const response = await fetch(`/api/translate/${encodeURIComponent(cleanedWordForApi)}`);
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        const data = await response.json();
        
        return {
            definition: data.definition || "", // 保持这个字段，但它应该会是空的
            translation: data.translation || "未找到中文翻译。"
        };
    } catch (error) {
        console.error("获取单词翻译失败:", error);
        return { definition: "", translation: "（获取翻译失败）" };
    }
}

// --- 获取单词发音并播放 ---
async function playWordPronunciation(word) {
    if (!word) return;

    pronounceButton.disabled = true;
    pronounceButton.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
    try {
        const cleanedWordForApi = cleanWord(word);
        const response = await fetch(`/api/pronounce/${encodeURIComponent(cleanedWordForApi)}`);
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(`HTTP error! status: ${response.status}: ${errorData.detail}`);
        }
        const audioBlob = await response.blob();
        const audioUrl = URL.createObjectURL(audioBlob);
        audioPlayer.src = audioUrl;
        audioPlayer.play();
        audioPlayer.onended = () => {
            URL.revokeObjectURL(audioUrl);
            pronounceButton.disabled = false;
            pronounceButton.innerHTML = '<i class="fas fa-volume-up"></i>';
        };
    } catch (error) {
        console.error("获取或播放发音失败:", error);
        // 如果发音失败，只在解释栏显示错误信息，不影响单词显示
        resultDefinitionSpan.textContent = "发音失败: " + error.message; 
        // resultWordSpan.textContent = ""; // 不清除单词
        pronounceButton.disabled = false;
        pronounceButton.innerHTML = '<i class="fas fa-volume-up"></i>';
    }
}

// --- 加载文本并初始化打字界面 ---
async function loadText(filename, initialWordIndex = 0) {
    console.log(`尝试加载文本: ${filename}, 从索引: ${initialWordIndex}`); // 新增日志
    try {
        const response = await fetch(`/api/get_text/${encodeURIComponent(filename)}`);
        if (!response.ok) {
            throw new Error(`HTTP error! status: ${response.status}`);
        }
        const data = await response.json();
        originalText = data.content;
        originalWords = originalText.split(/[\s\n\r]+/).filter(word => word.length > 0); 
        currentLoadedFilename = filename;
        currentWordIndex = initialWordIndex;

        typingInput.value = '';
        typedInput = '';
        typingInput.disabled = false;
        pronounceButton.disabled = true;
        resultWordSpan.textContent = ""; // 清空之前显示的单词
        resultDefinitionSpan.textContent = ""; // 清空之前显示的解释
        
        renderOriginalText();
        typingInput.focus();
        alert(`文本 "${filename}" 加载成功！从第 ${initialWordIndex + 1} 个单词开始。`);

    } catch (error) {
        console.error("加载文本失败:", error);
        alert(`加载文本 "${filename}" 失败: ${error.message}`);
    }
}

// --- 保存进度 ---
async function saveProgress() {
    if (!currentLoadedFilename) {
        console.warn("未加载文本，无法保存进度。");
        return;
    }
    if (currentWordIndex >= originalWords.length) {
        console.log("文本已完成，无需保存进度。");
        return;
    }

    // **重要修改：发送JSON数据而不是FormData**
    const progressData = {
        current_text_filename: currentLoadedFilename,
        current_word_index: currentWordIndex
    };

    try {
        const response = await fetch("/api/save_progress/", {
            method: "POST",
            headers: {
                'Content-Type': 'application/json' // 明确告诉后端我们发送的是JSON
            },
            body: JSON.stringify(progressData) // 将JavaScript对象转换为JSON字符串
        });
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(`HTTP error! status: ${response.status}: ${errorData.detail}`);
        }
        const data = await response.json();
        console.log("进度保存成功:", data.message);
    } catch (error) {
        console.error("进度保存失败:", error);
    }
}

// --- 加载进度并自动加载文本（用于页面初次加载） ---
async function loadAndApplyProgress() {
    console.log("尝试加载并应用全局进度..."); // 新增日志
    try {
        const response = await fetch("/api/load_progress/");
        if (!response.ok) {
            console.warn("API调用 /api/load_progress/ 失败！状态码:", response.status); // 新增日志
            return false; 
        }
        const progress = await response.json();
        console.log("从后端加载到的全局进度数据:", progress); // 新增日志

        // 只有当文件名存在且索引大于0，并且当前页面还没有加载任何文本时才自动应用进度
        // 关键：访问后端返回的正确字段名 `current_text_filename` 和 `current_word_index`
        if (progress.current_text_filename && typeof progress.current_text_filename === 'string' && progress.current_word_index > 0 && originalWords.length === 0) { 
            // 调试文件名匹配
            const cleanedProgressFilename = progress.current_text_filename.trim().toLowerCase();
            const filenameFromUrl = new URLSearchParams(window.location.search).get('file'); // 如果是通过URL参数加载
            const cleanedFilenameFromUrl = filenameFromUrl ? filenameFromUrl.trim().toLowerCase() : '';

            // 检查URL参数中的文件名（如果存在）与进度文件名是否匹配
            if (cleanedProgressFilename === cleanedFilenameFromUrl) {
                // 使用正确的字段名来显示和加载
                const confirmLoad = confirm(`检测到上次学习 "${progress.current_text_filename}" 进行到第 ${progress.current_word_index + 1} 个单词。是否从此处继续？`);
                if (confirmLoad) {
                    console.log("用户选择继续全局进度，加载文件:", progress.current_text_filename, "从索引:", progress.current_word_index); // 新增日志
                    await loadText(progress.current_text_filename, progress.current_word_index);
                    return true;
                } else {
                    console.log("用户取消全局进度自动加载。"); // 新增日志
                }
            } else {
                console.log("全局进度文件名与URL参数文件名不匹配，不自动加载。");
            }
        }
    } catch (error) {
        console.error("加载全局进度失败:", error);
    }
    return false;
}

// --- 获取已上传文本列表并填充选择器 ---
async function populateTextSelector() {
    try {
        const response = await fetch('/api/list_texts/');
        if (response.ok) {
            const files = await response.json();
            textSelector.innerHTML = '<option value="">或选择已上传的文本</option>';
            files.forEach(filename => {
                const option = document.createElement('option');
                option.value = filename;
                option.textContent = filename;
                textSelector.appendChild(option);
            });
        } else {
            console.warn("无法获取文本列表，请确保后端 /api/list_texts 接口存在并正常工作。");
        }
    } catch (error) {
        console.error("填充文本选择器失败:", error);
    }
}


// --- 事件监听器 ---

typingInput.addEventListener('input', async (e) => {
    if (!originalText || originalWords.length === 0) {
        typingInput.value = '';
        resultWordSpan.textContent = "请先上传或选择文本！";
        resultDefinitionSpan.textContent = "";
        return;
    }
    if (currentWordIndex >= originalWords.length) { 
        typingInput.disabled = true;
        return;
    }

    typedInput = e.target.value;
    const currentTargetWord = originalWords[currentWordIndex];
    const cleanedTargetWord = cleanWord(currentTargetWord);
    const cleanedTypedInput = cleanWord(typedInput);

    const isWordEnded = typedInput.endsWith(' ') || typedInput.endsWith('\n') || 
                         typedInput.endsWith('.') || typedInput.endsWith(',') || 
                         typedInput.endsWith('?') || typedInput.endsWith('!');

    if (isWordEnded && cleanedTypedInput === cleanedTargetWord) {
        typingInput.classList.add('correct-input-border');
        typingInput.classList.remove('incorrect-input-border');
        
        currentWordToPronounce = currentTargetWord;
        
        await playWordPronunciation(currentWordToPronounce);

        // 只解构 translation，因为我们只用它
        const { translation } = await getWordTranslationAndDefinition(currentTargetWord); 
        
        // 只显示中文翻译，并处理可能的错误信息
        if (translation && translation !== "无效单词" && !translation.includes("（翻译服务出错") && !translation.includes("（后端翻译功能未启用")) {
             resultDefinitionSpan.textContent = translation; 
        } else if (translation.includes("（翻译服务出错") || translation.includes("（后端翻译功能未启用")) {
             resultDefinitionSpan.textContent = translation; // 显示后端返回的错误信息
        }
        else {
             resultDefinitionSpan.textContent = "未找到中文翻译。"; // 如果翻译为空或无效
        }
        
        resultWordSpan.textContent = currentTargetWord; // 单词仍然显示在这里

        pronounceButton.disabled = false;

        currentWordIndex++;
        typingInput.value = '';
        typedInput = '';

        await saveProgress();

        if (currentWordIndex >= originalWords.length) {
            resultDefinitionSpan.textContent = "文本已完成！";
            resultWordSpan.textContent = "恭喜你！";
            typingInput.disabled = true;
            pronounceButton.disabled = true;
            await saveProgress();
        }
        renderOriginalText(); 
    } else if (cleanedTypedInput === cleanedTargetWord.substring(0, cleanedTypedInput.length)) {
        typingInput.classList.remove('incorrect-input-border');
        typingInput.classList.remove('correct-input-border');
        resultWordSpan.textContent = "";
        resultDefinitionSpan.textContent = "";
        pronounceButton.disabled = true;
    } else {
        typingInput.classList.add('incorrect-input-border');
        typingInput.classList.remove('correct-input-border');
        resultWordSpan.textContent = "";
        resultDefinitionSpan.textContent = "";
        pronounceButton.disabled = true;
    }
});

pronounceButton.addEventListener('click', () => {
    if (currentWordToPronounce) {
        playWordPronunciation(currentWordToPronounce);
    }
});

fileUploadInput.addEventListener('change', () => {
    if (fileUploadInput.files.length > 0) {
        uploadButton.textContent = `上传 ${fileUploadInput.files[0].name}`;
        uploadButton.disabled = false;
    } else {
        uploadButton.textContent = '上传并开始';
        uploadButton.disabled = true;
    }
});

uploadButton.addEventListener('click', async () => {
    const file = fileUploadInput.files[0];
    if (!file) {
        alert("请先选择一个文件。");
        return;
    }

    const formData = new FormData();
    formData.append("file", file);

    try {
        const response = await fetch("/api/upload_text/", {
            method: "POST",
            body: formData,
        });
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(`文件上传失败: ${errorData.detail}`);
        }
        const data = await response.json();
        alert(data.message);
        await loadText(data.filename, 0); // 上传成功后立即加载这个文本，并从头开始
        populateTextSelector();
        fileUploadInput.value = '';
        uploadButton.textContent = '上传并开始';
        uploadButton.disabled = true;
    } catch (error) {
        console.error("文件上传失败:", error);
        alert(`文件上传失败: ${error.message}`);
    }
});

loadTextButton.addEventListener('click', async () => {
    const selectedFilename = textSelector.value;
    if (selectedFilename) {
        console.log("点击加载文本按钮，选择的文件是:", selectedFilename); // 调试日志
        try {
            const response = await fetch("/api/load_progress/");
            if (!response.ok) {
                // 如果后端返回的不是200 OK，这里会抛出错误
                const errorData = await response.json(); // 尝试解析错误响应
                console.warn("API调用 /api/load_progress/ 失败！状态码:", response.status, "错误详情:", errorData); // 改进日志
                throw new Error(`加载进度失败，HTTP状态码: ${response.status}. 详情: ${errorData.detail || JSON.stringify(errorData)}`);
            }
            const progress = await response.json();
            console.log("从后端加载到的进度数据:", progress); // 调试日志

            // 核心逻辑判断 - 【这里是主要修改】
            // 访问后端返回的正确字段名 current_text_filename
            const cleanedProgressFilename = progress.current_text_filename && typeof progress.current_text_filename === 'string'
                                            ? progress.current_text_filename.trim().toLowerCase()
                                            : ''; // 如果不存在或不是字符串，则视为空字符串
            
            const cleanedSelectedFilename = selectedFilename.trim().toLowerCase();
            
            // 确保使用正确的字段名 current_word_index 来获取单词索引
            const hasValidIndex = (progress.current_word_index > 0); 

            // --- 新增调试日志：输出长度和每个字符的ASCII码 ---
            console.log(`调试信息：`);
            console.log(` cleanedProgressFilename (来自后端): "${cleanedProgressFilename}" (长度: ${cleanedProgressFilename.length})`);
            console.log(`   字符及ASCII码: ${Array.from(cleanedProgressFilename).map(char => `${char}(${char.charCodeAt(0)})`).join(', ')}`);
            
            console.log(` cleanedSelectedFilename (来自前端选择器): "${cleanedSelectedFilename}" (长度: ${cleanedSelectedFilename.length})`);
            console.log(`   字符及ASCII码: ${Array.from(cleanedSelectedFilename).map(char => `${char}(${char.charCodeAt(0)})`).join(', ')}`);
            // --- 调试日志结束 ---

            const isFilenameMatched = (cleanedProgressFilename === cleanedSelectedFilename);
            console.log(`文件名匹配: ${isFilenameMatched}, 进度索引有效: ${hasValidIndex}`); // 调试日志
            
            if (isFilenameMatched && hasValidIndex) {
                // 使用正确的字段名来显示和加载
                const confirmMessage = `检测到您之前在 "${progress.current_text_filename}" 中学习到第 ${progress.current_word_index + 1} 个单词。是否从此处继续？`;
                console.log("将弹出确认框，消息:", confirmMessage); // 调试日志
                const confirmLoad = confirm(confirmMessage);
                
                if (confirmLoad) {
                    console.log("用户点击了确认。加载文件:", progress.current_text_filename, "从索引:", progress.current_word_index); // 调试日志
                    await loadText(progress.current_text_filename, progress.current_word_index); 
                    return; 
                } else {
                    console.log("用户点击了取消。将从第 1 个单词开始加载。"); // 调试日志
                }
            } else {
                console.log("文件名不匹配或进度索引无效。将从第 1 个单词开始加载。"); // 调试日志
            }
            
            await loadText(selectedFilename, 0); // 从头开始加载

        } catch (error) {
            console.error("加载文本或处理进度时发生错误:", error.message || error); // **关键修改：打印 error.message**
            alert(`加载文本 "${selectedFilename}" 失败，将从头开始：${error.message || error}`);
            await loadText(selectedFilename, 0);
        }
    } else {
        alert("请选择一个已上传的文本文件。");
    }
});

window.addEventListener('beforeunload', async (event) => {
    // 异步保存进度
    if (currentLoadedFilename && currentWordIndex < originalWords.length) {
        // 为了确保在页面关闭前保存完成，这里可以做一个简单的同步等待或者不阻止页面关闭
        // 浏览器通常会阻止同步的XHR请求在beforeunload中，但这里是异步的
        // 最好的做法是依赖后端日志确认是否保存成功
        saveProgress(); 
    }
});

document.addEventListener('DOMContentLoaded', async () => {
    await populateTextSelector(); // 填充文本选择器
    const progressLoaded = await loadAndApplyProgress(); // 尝试加载上次的全局进度
    if (!progressLoaded && originalWords.length === 0) {
        // 如果没有加载到进度，且当前没有文本，则显示提示
        resultDefinitionSpan.textContent = "请上传或选择文本开始学习！";
        resultWordSpan.textContent = "";
    }
    uploadButton.disabled = true; // 初始时上传按钮应为禁用
});