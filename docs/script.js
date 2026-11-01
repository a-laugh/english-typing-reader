// 键学 · 静态站点逻辑（对话式打字流）
// 数据全部来自构建期烘焙的 docs/data/episodes/（无后端、零运行时网络依赖），
// 多用户各自浏览器 localStorage 保存进度，互不干扰。

// ---------------------------------------------------------------------------
// 常量与状态
// ---------------------------------------------------------------------------

const DATA_BASE = "data/episodes";
const INDEX_URL = `${DATA_BASE}/index.json`;
const AUDIO_BASE = "data/audio"; // 构建期 TTS 音频，跨剧集共享（与 fetcher.py AUDIO_DIR 对应）
const WINDOW_DAYS = 30; // 与 fetcher.py 的滚动窗口保持一致
const DISPLAY_WORDS_AROUND_CURRENT = 60;

const LS_PROGRESS_PREFIX = "typelearning:progress:";
const LS_LAST_EPISODE = "typelearning:last_episode";
const PROGRESS_VERSION = 1;

let episodeIndex = [];       // index.json：[{id, title, pub_date, bbc_url}]
let currentEpisode = null;   // 已加载的完整剧集 JSON
let episodeTurns = [];       // 当前剧集回合（或由 transcript 兜底的单一伪回合）
let originalWords = [];      // 全剧集展平的单词流（回合文本按序拼接，与进度下标对应）
let turnIndexByWord = [];    // 每个词所属的回合下标
let currentWordIndex = 0;
let glossWordIndex = -1;     // 行内翻译跟随的最新完成词，-1 = 无

// DOM 引用
const episodeSelector = document.getElementById("episode-selector");
const episodeMeta = document.getElementById("episode-meta");
const noticeBar = document.getElementById("notice");
const dialogDisplay = document.getElementById("dialog-display");
const typingInput = document.getElementById("typing-input");
const hintCurrent = document.getElementById("hint-current");
const hintProgress = document.getElementById("hint-progress");

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

function cleanWord(word) {
    return word.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function showNotice(message) {
    noticeBar.textContent = message;
    noticeBar.hidden = false;
}

function clearNotice() {
    noticeBar.hidden = true;
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

// 垃圾回收：剧集滚动下线后，清掉指向它们的进度条目，存储不无限膨胀
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
        option.textContent = "暂无节目（还没构建，请稍后再来）";
        episodeSelector.appendChild(option);
        episodeSelector.disabled = true;
        return;
    }
    episodeSelector.disabled = false;
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

function renderEpisodeMeta(entry) {
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
    const bbcLink = document.createElement("a");
    // 构建期数据视为外部输入：链接仅放行 BBC 官方域名，防 javascript: 等注入
    if (/^https:\/\/(www\.)?bbc\.(co\.uk|com)\//.test(entry.bbc_url)) {
        bbcLink.href = entry.bbc_url;
        bbcLink.target = "_blank";
        bbcLink.rel = "noopener";
        bbcLink.textContent = "看原文 ↗";
    }
    episodeMeta.append(published, bbcLink);

    const progress = readProgress(entry.id);
    if (progress && !progress.done && progress.word_index > 0 && currentEpisode) {
        const restartLink = document.createElement("a");
        restartLink.href = "#";
        restartLink.textContent = "↺ 重新开始";
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
// 发音：优先播放构建期烘焙的本地 MP3（零延迟、零网络依赖），
// 无烘焙音频（构建时 TTS 失败/关闭）时回退浏览器 Web Speech API
// ---------------------------------------------------------------------------

const audioCache = new Map(); // 清洗后单词 -> HTMLAudioElement | null（null=无烘焙音频）

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

function speakViaTts(cleaned) {
    if (!window.speechSynthesis) return;
    speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(cleaned);
    utterance.lang = "en-US";
    utterance.rate = 0.95;
    if (!ttsVoiceReady) pickVoice();
    if (ttsVoice) utterance.voice = ttsVoice;
    speechSynthesis.speak(utterance);
}

function primeAudio(cleaned) {
    // 为该词建一个加载中的 Audio 元素（preload=auto 即开始后台下载）；
    // 404 时 error 事件把缓存改写为 null，之后直接走 TTS 兜底
    const audio = new Audio();
    audio.preload = "auto";
    audio.addEventListener("error", () => audioCache.set(cleaned, null), { once: true });
    audio.src = `${AUDIO_BASE}/${encodeURIComponent(cleaned)}.mp3`;
    audioCache.set(cleaned, audio);
    audio.load();
    return audio;
}

// 当前正在播放的 MP3：快速连打时先停掉上一个，避免多词重叠发声
let activeAudio = null;

async function speakWord(word) {
    const cleaned = cleanWord(word);
    if (!cleaned) return;
    if (activeAudio && !activeAudio.paused) {
        activeAudio.pause();
    }
    const cached = audioCache.get(cleaned);
    if (cached === null) {
        // 已知无烘焙音频（构建期生成失败/缺失）
        speakViaTts(cleaned);
        return;
    }
    const audio = cached instanceof HTMLAudioElement ? cached : primeAudio(cleaned);
    try {
        audio.currentTime = 0;
        // 直接 play()：流式起播，首字节到达即出声——不等 canplaythrough，
        // 否则冷启动时（国内访问 github.io 带宽差）要等整个文件下载完才发声
        await audio.play();
        activeAudio = audio;
    } catch (e) {
        // 404 / 加载失败 / 自动播放策略限制 → 浏览器 TTS 兜底
        speakViaTts(cleaned);
    }
}

function prefetchUpcomingAudio(count = 8) {
    // 预取接下来 count 个未缓存的词：单个词的冷下载最坏可达数秒，
    // 只预取下一个词会被打字速度追上；窗口化预取让慢速下载有时间提前完成
    let need = count;
    for (let i = currentWordIndex; i < originalWords.length && need > 0; i++) {
        const w = cleanWord(originalWords[i]);
        if (w && !audioCache.has(w)) {
            primeAudio(w);
            need--;
        }
    }
}

// ---------------------------------------------------------------------------
// 释义查询（构建期烘焙词典，本地同步查询）
// ---------------------------------------------------------------------------

function lookupWord(token) {
    const dict = currentEpisode ? currentEpisode.dict : {};
    return dict[token.toLowerCase()] || dict[cleanWord(token)] || null;
}

// 行内翻译元素：紧跟最新完成的单词；语境层带 📖，官方层带「官方」小标
function buildGloss(token) {
    const entry = lookupWord(token);
    const span = document.createElement("span");
    if (!entry) {
        span.className = "gloss";
        span.textContent = "（未收录）";
        return span;
    }
    if (entry.source === "context") {
        span.className = "gloss ctx";
        span.textContent = `📖 ${entry.context || entry.translation}`;
        return span;
    }
    span.className = "gloss";
    if (entry.source === "official") {
        const tag = document.createElement("span");
        tag.className = "gloss-tag";
        tag.textContent = "官方";
        span.appendChild(tag);
    }
    span.appendChild(document.createTextNode(entry.translation || entry.phrase || ""));
    return span;
}

// ---------------------------------------------------------------------------
// 对话式渲染（窗口化：当前词 ±DISPLAY_WORDS_AROUND_CURRENT 覆盖的整回合）
// ---------------------------------------------------------------------------

const SPEAKER_CLASSES = ["speaker-c0", "speaker-c1", "speaker-c2", "speaker-c3", "speaker-c4"];

function speakerClass(name) {
    let h = 0;
    for (const ch of name) h = (h * 31 + ch.codePointAt(0)) >>> 0;
    return SPEAKER_CLASSES[h % SPEAKER_CLASSES.length];
}

function flattenEpisode() {
    // 兼容无 turns 的旧数据：由 transcript 构造单一伪回合
    episodeTurns = currentEpisode.turns && currentEpisode.turns.length
        ? currentEpisode.turns
        : [{ speaker: "", text: currentEpisode.transcript }];
    originalWords = [];
    turnIndexByWord = [];
    episodeTurns.forEach((turn, ti) => {
        turn.text.split(/\s+/).filter(Boolean).forEach((w) => {
            originalWords.push(w);
            turnIndexByWord.push(ti);
        });
    });
}

function renderDialog() {
    const from = turnIndexByWord[Math.max(0, currentWordIndex - DISPLAY_WORDS_AROUND_CURRENT)] ?? 0;
    const to = turnIndexByWord[Math.min(originalWords.length - 1, currentWordIndex + DISPLAY_WORDS_AROUND_CURRENT)]
        ?? turnIndexByWord[turnIndexByWord.length - 1] ?? 0;

    // DOM API + textContent 构建：剧集数据来自外部站点，防存储型 XSS
    const frag = document.createDocumentFragment();
    let wordIdx = 0;
    episodeTurns.forEach((turn, ti) => {
        const words = turn.text.split(/\s+/).filter(Boolean);
        const turnStart = wordIdx;
        wordIdx += words.length;
        if (ti < from || ti > to) return;

        const row = document.createElement("div");
        row.className = "turn";
        if (turn.speaker) {
            const badge = document.createElement("span");
            badge.className = `speaker-badge ${speakerClass(turn.speaker)}`;
            badge.textContent = turn.speaker;
            row.appendChild(badge);
        }
        const para = document.createElement("p");
        para.className = "turn-text";
        words.forEach((w, k) => {
            const gi = turnStart + k;
            const span = document.createElement("span");
            if (gi < currentWordIndex) {
                span.className = "word-correct";
            } else if (gi === currentWordIndex) {
                span.className = "current-word";
            }
            span.textContent = w;
            para.appendChild(span);
            para.appendChild(document.createTextNode(" "));
            if (gi === glossWordIndex) {
                para.appendChild(buildGloss(w));
            }
        });
        row.appendChild(para);
        frag.appendChild(row);
    });
    dialogDisplay.replaceChildren(frag);

    const currentWordSpan = dialogDisplay.querySelector(".current-word");
    if (currentWordSpan) {
        const displayRect = dialogDisplay.getBoundingClientRect();
        const wordRect = currentWordSpan.getBoundingClientRect();
        const visible = wordRect.top >= displayRect.top && wordRect.bottom <= displayRect.bottom;
        if (!visible) {
            currentWordSpan.scrollIntoView({ behavior: "smooth", block: "center" });
        }
    }
}

function renderHint() {
    if (!currentEpisode || originalWords.length === 0) {
        hintCurrent.textContent = "";
        hintProgress.textContent = "";
        return;
    }
    if (currentWordIndex >= originalWords.length) {
        hintCurrent.textContent = "🎉 本期节目已完成";
        hintProgress.textContent = `进度 ${originalWords.length}/${originalWords.length} 词`;
        return;
    }
    hintCurrent.replaceChildren();
    hintCurrent.append("当前词 ");
    const hw = document.createElement("span");
    hw.className = "hw";
    hw.textContent = originalWords[currentWordIndex];
    hintCurrent.appendChild(hw);
    hintCurrent.append("（打完按空格）");
    hintProgress.textContent = `进度 ${currentWordIndex}/${originalWords.length} 词`;
}

// ---------------------------------------------------------------------------
// 加载与打字主流程
// ---------------------------------------------------------------------------

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
        flattenEpisode();

        // startIndex 为 null 表示自动续读：有未完成进度则接着上次，否则从头
        let start = startIndex;
        if (start === null) {
            const progress = readProgress(episodeId);
            start = progress && !progress.done && progress.word_index > 0
                ? progress.word_index : 0;
        }
        currentWordIndex = Math.min(start, Math.max(0, originalWords.length - 1));
        glossWordIndex = currentWordIndex > 0 ? currentWordIndex - 1 : -1;

        typingInput.disabled = false;
        typingInput.value = "";
        renderDialog();
        renderHint();
        renderEpisodeMeta(currentEpisode);
        prefetchUpcomingAudio();
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
        hintCurrent.textContent = "请先选择节目！";
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
        currentWordIndex++;
        typingInput.value = "";
        glossWordIndex = currentWordIndex - 1; // 行内翻译跟随最新完成的词
        renderDialog();
        renderHint();

        speakWord(completedWord);
        prefetchUpcomingAudio();

        if (currentWordIndex >= originalWords.length) {
            glossWordIndex = -1;
            renderDialog();
            typingInput.disabled = true;
            writeProgress(currentEpisode.id, currentWordIndex, true);
        } else {
            writeProgress(currentEpisode.id, currentWordIndex);
        }
        renderEpisodeMeta(currentEpisode);
    } else if (cleanedTypedInput === cleanedTargetWord.substring(0, cleanedTypedInput.length)) {
        typingInput.classList.remove("incorrect-input-border");
        typingInput.classList.remove("correct-input-border");
    } else {
        typingInput.classList.add("incorrect-input-border");
        typingInput.classList.remove("correct-input-border");
    }
});

episodeSelector.addEventListener("change", () => {
    clearNotice();
    // 选择即加载（含自动续读），无需额外按钮
    if (episodeSelector.value) {
        loadEpisode(episodeSelector.value);
    }
});

// ---------------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------------

async function init() {
    if (!window.speechSynthesis) {
        console.warn("当前浏览器不支持语音合成（Web Speech API），音频缺失的词将无法发音。");
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
    if (!resumed && noticeBar.hidden) {
        hintCurrent.textContent = "请选择节目开始学习！";
    }
}

document.addEventListener("DOMContentLoaded", init);
