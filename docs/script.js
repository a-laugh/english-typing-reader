// 键学 · 静态站点逻辑
// 数据全部来自构建期烘焙的 docs/data/episodes/（无后端、零运行时网络依赖），
// 多用户各自浏览器 localStorage 保存进度，互不干扰。

// ---------------------------------------------------------------------------
// 常量与状态
// ---------------------------------------------------------------------------

const DATA_BASE = "data/episodes";
const INDEX_URL = `${DATA_BASE}/index.json`;
const WINDOW_DAYS = 30; // 与 fetcher.py 的滚动窗口保持一致
const DISPLAY_WORDS_AROUND_CURRENT = 30;

const LS_PROGRESS_PREFIX = "typelearning:progress:";
const LS_LAST_EPISODE = "typelearning:last_episode";
const PROGRESS_VERSION = 1;

let episodeIndex = [];       // index.json：[{id, title, pub_date, bbc_url}]
let currentEpisode = null;   // 已加载的完整剧集 JSON
let originalWords = [];
let currentWordIndex = 0;
let currentWordToPronounce = "";

// DOM 引用
const episodeSelector = document.getElementById("episode-selector");
const loadEpisodeButton = document.getElementById("load-episode-btn");
const episodeMeta = document.getElementById("episode-meta");
const noticeBar = document.getElementById("notice");
const originalTextDisplay = document.getElementById("original-text-display");
const typingInput = document.getElementById("typing-input");
const resultWordSpan = document.querySelector("#result-display .result-word");
const resultTranslationSpan = document.querySelector("#result-display .result-translation");
const resultContextSpan = document.querySelector("#result-display .result-context");
const resultSentenceSpan = document.querySelector("#result-display .result-sentence");
const pronounceButton = document.getElementById("pronounce-button");

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

function cleanWord(word) {
    return word.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function showNotice(message) {
    noticeBar.textContent = message;
    noticeBar.style.display = "block";
}

function clearNotice() {
    noticeBar.style.display = "none";
}

function daysBetween(fromIso, toTime = Date.now()) {
    const from = new Date(`${fromIso}T00:00:00`);
    return Math.floor((toTime - from.getTime()) / 86400000);
}

// ---------------------------------------------------------------------------
// localStorage 进度（滚动窗口兼容）
// ---------------------------------------------------------------------------

function readProgress(episodeId) {
    try {
        const raw = localStorage.getItem(LS_PROGRESS_PREFIX + episodeId);
        if (!raw) return null;
        const data = JSON.parse(raw);
        if (data && data.v === PROGRESS_VERSION && Number.isInteger(data.word_index)) {
            return data;
        }
    } catch (e) { /* 损坏的条目视作无进度 */ }
    return null;
}

function writeProgress(episodeId, wordIndex, done = false) {
    try {
        localStorage.setItem(LS_PROGRESS_PREFIX + episodeId, JSON.stringify({
            v: PROGRESS_VERSION,
            word_index: wordIndex,
            done,
            updated_at: new Date().toISOString(),
        }));
        localStorage.setItem(LS_LAST_EPISODE, episodeId);
    } catch (e) {
        console.warn("进度写入 localStorage 失败:", e);
    }
}

// 垃圾回收：剧集 30 天滚动下线后，清掉指向它们的进度条目，存储不无限膨胀
function gcProgress() {
    const alive = new Set(episodeIndex.map((e) => e.id));
    const dead = [];
    for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (key && key.startsWith(LS_PROGRESS_PREFIX)) {
            const id = key.slice(LS_PROGRESS_PREFIX.length);
            if (!alive.has(id)) {
                dead.push(key);
            }
        }
    }
    dead.forEach((key) => localStorage.removeItem(key));
    return dead.length;
}

// ---------------------------------------------------------------------------
// 剧集列表
// ---------------------------------------------------------------------------

function selectorLabel(entry) {
    const progress = readProgress(entry.id);
    const doneMark = progress && progress.done ? "✓ " : "";
    return `${doneMark}${entry.title}（${entry.pub_date.slice(5).replace("-", "/")}）`;
}

function populateEpisodeSelector() {
    episodeSelector.innerHTML = "";
    if (episodeIndex.length === 0) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "暂无节目（今天还没构建，请稍后再来）";
        episodeSelector.appendChild(option);
        episodeSelector.disabled = true;
        loadEpisodeButton.disabled = true;
        return;
    }
    episodeSelector.disabled = false;
    loadEpisodeButton.disabled = false;
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = `选择节目（最近 ${WINDOW_DAYS} 天）`;
    episodeSelector.appendChild(placeholder);
    episodeIndex.forEach((entry) => {
        const option = document.createElement("option");
        option.value = entry.id;
        option.textContent = selectorLabel(entry);
        episodeSelector.appendChild(option);
    });
}

function renderEpisodeMeta(entry, wordIndex, totalWords) {
    episodeMeta.replaceChildren();
    const published = document.createElement("span");
    published.textContent = `${entry.pub_date} 发布`;
    const remain = WINDOW_DAYS - 1 - daysBetween(entry.pub_date);
    if (remain >= 0) {
        const remainTag = document.createElement("span");
        remainTag.className = "remaining-days";
        remainTag.textContent = remain > 0 ? `剩 ${remain} 天` : "今日下线";
        published.appendChild(remainTag);
    }
    const progressText = document.createElement("span");
    progressText.textContent = `　进度 ${wordIndex}/${totalWords} 词`;
    const bbcLink = document.createElement("a");
    // 构建期数据视为外部输入：链接仅放行 BBC 官方域名，防 javascript: 等注入
    if (/^https:\/\/(www\.)?bbc\.(co\.uk|com)\//.test(entry.bbc_url)) {
        bbcLink.href = entry.bbc_url;
        bbcLink.target = "_blank";
        bbcLink.rel = "noopener";
        bbcLink.className = "bbc-link";
        bbcLink.textContent = "　看原文";
    }
    episodeMeta.append(published, progressText, bbcLink);

    const progress = readProgress(entry.id);
    if (progress && !progress.done && progress.word_index > 0 && currentEpisode) {
        const restartLink = document.createElement("a");
        restartLink.href = "#";
        restartLink.className = "bbc-link";
        restartLink.textContent = "　↺ 重新开始";
        restartLink.addEventListener("click", (e) => {
            e.preventDefault();
            if (confirm("确定清除本节目的进度，从第 1 个单词重新开始吗？")) {
                loadEpisode(entry.id, 0);
            }
        });
        episodeMeta.appendChild(restartLink);
    }
}

// ---------------------------------------------------------------------------
// 发音（浏览器 Web Speech API）
// ---------------------------------------------------------------------------

let ttsVoice = null;
let ttsVoiceReady = false;

function pickVoice() {
    ttsVoiceReady = true;
    const voices = window.speechSynthesis ? speechSynthesis.getVoices() : [];
    if (!voices.length) return;
    const english = voices.filter((v) => v.lang && v.lang.toLowerCase().startsWith("en"));
    const pool = english.length ? english : voices;
    // 优先本地语音（离线可用、国内无网络依赖），再优先美音与自然音色
    ttsVoice =
        pool.find((v) => v.localService && /en[-_]us/i.test(v.lang) && /natural|aria|guy|zira|david/i.test(v.name)) ||
        pool.find((v) => v.localService && /en[-_]us/i.test(v.lang)) ||
        pool.find((v) => v.localService) ||
        pool.find((v) => /en[-_]us/i.test(v.lang)) ||
        pool[0];
}

function speakWord(word) {
    if (!window.speechSynthesis) return;
    const cleaned = cleanWord(word);
    if (!cleaned) return;
    speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(cleaned);
    utterance.lang = "en-US";
    utterance.rate = 0.95;
    if (!ttsVoiceReady) pickVoice();
    if (ttsVoice) utterance.voice = ttsVoice;
    speechSynthesis.speak(utterance);
}

// ---------------------------------------------------------------------------
// 释义查询（构建期烘焙词典，本地同步查询）
// ---------------------------------------------------------------------------

function lookupWord(token) {
    const dict = currentEpisode ? currentEpisode.dict : {};
    return dict[token.toLowerCase()] || dict[cleanWord(token)] || null;
}

function renderWordResult(token) {
    const entry = lookupWord(token);
    resultWordSpan.replaceChildren();
    resultTranslationSpan.replaceChildren();
    resultContextSpan.replaceChildren();
    resultSentenceSpan.replaceChildren();

    const cleaned = cleanWord(token) || token;
    resultWordSpan.textContent = cleaned;

    if (!entry) {
        resultTranslationSpan.textContent = "（本词未收录释义）";
        return;
    }

    const badge = document.createElement("span");
    const labels = { official: "官方词汇", context: "语境", ecdict: "词典" };
    badge.className = `source-badge source-${entry.source}`;
    badge.textContent = labels[entry.source] || entry.source;

    const phrasePrefix = entry.phrase && entry.phrase !== cleaned ? `${entry.phrase} · ` : "";
    resultTranslationSpan.append(`${phrasePrefix}${entry.translation || "（无释义）"}`);
    resultTranslationSpan.appendChild(badge);

    if (entry.context) {
        resultContextSpan.textContent = `📖 语境释义：${entry.context}`;
    }
    if (entry.sentence) {
        resultSentenceSpan.textContent = `“${entry.sentence}”`;
    }
}

function clearWordResult() {
    resultWordSpan.textContent = "";
    resultTranslationSpan.textContent = "";
    resultContextSpan.textContent = "";
    resultSentenceSpan.textContent = "";
}

// ---------------------------------------------------------------------------
// 打字主流程（沿用原实现的窗口渲染与完词判定）
// ---------------------------------------------------------------------------

function renderOriginalText() {
    const startIndex = Math.max(0, currentWordIndex - DISPLAY_WORDS_AROUND_CURRENT);
    const endIndex = Math.min(originalWords.length, currentWordIndex + DISPLAY_WORDS_AROUND_CURRENT);

    // DOM API + textContent 构建：剧集数据来自外部站点，防存储型 XSS
    const frag = document.createDocumentFragment();
    for (let i = startIndex; i < endIndex; i++) {
        const word = originalWords[i];
        let spanClass = "";
        if (i < currentWordIndex) {
            spanClass = "word-correct";
        } else if (i === currentWordIndex) {
            spanClass = "current-word";
        }
        const span = document.createElement("span");
        if (spanClass) span.className = spanClass;
        span.textContent = word;
        frag.appendChild(span);
        frag.appendChild(document.createTextNode(" "));
    }
    originalTextDisplay.replaceChildren(frag);

    const currentWordSpan = originalTextDisplay.querySelector(".current-word");
    if (currentWordSpan) {
        const displayRect = originalTextDisplay.getBoundingClientRect();
        const wordRect = currentWordSpan.getBoundingClientRect();
        const visible = wordRect.top >= displayRect.top && wordRect.bottom <= displayRect.bottom;
        if (!visible) {
            currentWordSpan.scrollIntoView({ behavior: "smooth", block: "center" });
        }
    }
}

async function loadEpisode(episodeId, startIndex = null) {
    clearNotice();
    try {
        // no-cache = 每次回源校验（命中 304 依然便宜）：
        // 剧集每周更新/滚动过期，不能吃浏览器的启发式缓存，否则用户会看到旧的列表
        const response = await fetch(`${DATA_BASE}/${encodeURIComponent(episodeId)}.json`, {
            cache: "no-cache",
        });
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        currentEpisode = await response.json();
        originalWords = currentEpisode.transcript.split(/\s+/).filter((w) => w.length > 0);

        // startIndex 为 null 表示自动续读：有未完成进度则接着上次，否则从头
        let start = startIndex;
        if (start === null) {
            const progress = readProgress(episodeId);
            start = progress && !progress.done && progress.word_index > 0
                ? progress.word_index : 0;
        }
        currentWordIndex = Math.min(start, Math.max(0, originalWords.length - 1));

        typingInput.disabled = false;
        typingInput.value = "";
        pronounceButton.disabled = true;
        currentWordToPronounce = "";
        clearWordResult();
        renderOriginalText();
        renderEpisodeMeta(currentEpisode, currentWordIndex, originalWords.length);
        typingInput.focus();
    } catch (error) {
        console.error("加载节目失败:", error);
        showNotice(`节目加载失败（${error.message}）。可能刚过 30 天下线了，请选择其他节目。`);
        populateEpisodeSelector();
    }
}

typingInput.addEventListener("input", (e) => {
    if (!currentEpisode || originalWords.length === 0) {
        typingInput.value = "";
        resultTranslationSpan.textContent = "请先选择节目！";
        return;
    }
    if (currentWordIndex >= originalWords.length) {
        typingInput.disabled = true;
        return;
    }

    const typedInput = e.target.value;
    const currentTargetWord = originalWords[currentWordIndex];
    const cleanedTargetWord = cleanWord(currentTargetWord);
    const cleanedTypedInput = cleanWord(typedInput);

    const isWordEnded = typedInput.endsWith(" ") || typedInput.endsWith("\n") ||
                        typedInput.endsWith(".") || typedInput.endsWith(",") ||
                        typedInput.endsWith("?") || typedInput.endsWith("!") ||
                        typedInput.endsWith(";") || typedInput.endsWith(":");

    if (isWordEnded && cleanedTypedInput === cleanedTargetWord) {
        typingInput.classList.add("correct-input-border");
        typingInput.classList.remove("incorrect-input-border");

        const completedWord = currentTargetWord;
        currentWordToPronounce = completedWord;
        currentWordIndex++;
        typingInput.value = "";
        renderOriginalText();

        speakWord(completedWord);
        renderWordResult(completedWord);
        pronounceButton.disabled = false;

        if (currentWordIndex >= originalWords.length) {
            resultWordSpan.textContent = "恭喜你！";
            resultTranslationSpan.textContent = "本期节目已完成！";
            resultContextSpan.textContent = "";
            resultSentenceSpan.textContent = "";
            typingInput.disabled = true;
            writeProgress(currentEpisode.id, currentWordIndex, true);
        } else {
            writeProgress(currentEpisode.id, currentWordIndex);
        }
        renderEpisodeMeta(currentEpisode, currentWordIndex, originalWords.length);
    } else if (cleanedTypedInput === cleanedTargetWord.substring(0, cleanedTypedInput.length)) {
        typingInput.classList.remove("incorrect-input-border");
        typingInput.classList.remove("correct-input-border");
        clearWordResult();
        pronounceButton.disabled = true;
    } else {
        typingInput.classList.add("incorrect-input-border");
        typingInput.classList.remove("correct-input-border");
        clearWordResult();
        pronounceButton.disabled = true;
    }
});

pronounceButton.addEventListener("click", () => {
    if (currentWordToPronounce) {
        speakWord(currentWordToPronounce);
    }
});

loadEpisodeButton.addEventListener("click", () => {
    const selectedId = episodeSelector.value;
    if (selectedId) {
        // 未指定起点：有未完成进度则自动续读，从头开始请用 meta 栏的「↺ 重新开始」
        loadEpisode(selectedId);
    }
});

episodeSelector.addEventListener("change", () => {
    clearNotice();
});

// ---------------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------------

async function init() {
    if (!window.speechSynthesis) {
        showNotice("当前浏览器不支持语音合成（Web Speech API），单词将无法发音。建议使用 Chrome / Edge / Safari。");
    } else {
        pickVoice();
        speechSynthesis.onvoiceschanged = pickVoice;
    }

    let indexLoaded = false;
    try {
        const response = await fetch(INDEX_URL, { cache: "no-cache" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        // 归一化：200 + 非数组 JSON（劫持层/错误对象）按加载失败处理，
        // 避免下游 forEach 抛错中断 init
        const data = await response.json();
        if (!Array.isArray(data)) throw new Error("index.json 不是数组");
        indexLoaded = true;
        episodeIndex = data;
    } catch (error) {
        console.error("加载节目列表失败:", error);
        indexLoaded = false;
        episodeIndex = [];
        showNotice("节目列表加载失败，请刷新重试。");
    }

    populateEpisodeSelector();

    // 关键防线：index 未成功加载时绝不执行 GC 与续读判定——
    // 否则空列表会把所有 progress:* 当成「已下线」清空，一次网络抖动就丢光用户进度
    let resumed = false;
    if (indexLoaded) {
        // 滚动窗口垃圾回收：清掉已下线剧集的进度
        gcProgress();

        // 继续上次：仅当该剧仍在架上；已下线则提示并留在列表页
        try {
            const lastId = localStorage.getItem(LS_LAST_EPISODE);
            if (lastId) {
                const entry = episodeIndex.find((e) => e.id === lastId);
                const progress = readProgress(lastId);
                if (entry && progress && !progress.done && progress.word_index > 0) {
                    episodeSelector.value = lastId;
                    await loadEpisode(lastId, progress.word_index);
                    resumed = true;
                } else if (!entry) {
                    showNotice("上次的节目已下线（内容滚动保留 30 天），进度已清理。请选择最新节目。");
                    // 指针一并清掉，避免之后每次刷新都重复弹「已下线」提示
                    localStorage.removeItem(LS_LAST_EPISODE);
                }
            }
        } catch (e) {
            console.warn("恢复上次进度失败:", e);
        }
    }
    if (!resumed && noticeBar.style.display === "none") {
        resultTranslationSpan.textContent = "请选择节目开始学习！";
    }
}

document.addEventListener("DOMContentLoaded", init);
