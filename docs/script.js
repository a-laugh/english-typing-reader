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
let currentWordPos = 0;      // 当前词内已敲到的字母位（清洗序列下标）
let letterSpans = [];        // 当前词各字母位的 span（与清洗序列一一对应）
let glossWordIndex = -1;     // 行内翻译跟随的最新完成词，-1 = 无

// DOM 引用
const episodeSelector = document.getElementById("episode-selector");
const episodeMeta = document.getElementById("episode-meta");
const noticeBar = document.getElementById("notice");
const dialogDisplay = document.getElementById("dialog-display");
const kbdCapture = document.getElementById("kbd-capture");
const hintCurrent = document.getElementById("hint-current");
const hintProgress = document.getElementById("hint-progress");

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

function cleanWord(word) {
    return word.toLowerCase().replace(/[^a-z0-9]/g, "");
}

// 键入序列：剥离首尾非字母数字字符（句子标点自动通过），保留词内符号
// （we've 的撇号、six-minute 的连字符都需要键入）
function typingSequence(word) {
    let s = 0, e = word.length - 1;
    while (s <= e && !/[a-z0-9]/i.test(word[s])) s++;
    while (e >= s && !/[a-z0-9]/i.test(word[e])) e--;
    return word.slice(s, e + 1);
}

// 字符归一化：字母小写化；曲引号/破折号归一为键盤字符（BBC 文本用 ’ 而键盘是 '）
function normalizeChar(ch) {
    if (/[a-zA-Z]/.test(ch)) return ch.toLowerCase();
    const map = { "\u2019": "'", "\u2018": "'", "\u201C": '"', "\u201D": '"', "\u2013": "-", "\u2014": "-" };
    return map[ch] || ch;
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

// 内置基础词兜底词典（docs/assets/basics.json，~300 个最高频词）与
// 全局内建词表（docs/data/glossary.json，历史剧集挖掘 + 周构建自生长）：
// 释义查询链 = 剧集词典（LLM 语境）→ 全局词表 → 基础词表
let basicsDict = {};
let globalGlossary = {};

function lookupWord(token) {
    const dict = currentEpisode ? currentEpisode.dict : {};
    const entry = dict[token.toLowerCase()] || dict[cleanWord(token)];
    if (entry) {
        return entry;
    }
    const lower = token.toLowerCase();
    const global = globalGlossary[lower] || globalGlossary[cleanWord(token)];
    if (global) {
        return { translation: global, source: "global" };
    }
    const basic = basicsDict[cleanWord(token)];
    return basic ? { translation: basic, source: "basic" } : null;
}

// 简洁释义：取首个义项（按 ；/; 切分），中文 ~12 字符、英文 ~20 字符截断
function conciseTranslation(text) {
    if (!text) return "";
    const first = String(text).split(/[；;]/)[0].trim();
    const isAscii = /^[\x20-\x7F]+$/.test(first);
    const limit = isAscii ? 20 : 12;
    return first.length > limit ? first.slice(0, limit) + "…" : first;
}

// 行内翻译元素：显示在宿主词上方的浮层（绝对定位，零布局位移）；
// 语境层带 📖，官方层带「官方」小标。
// 无词条返回 null 不显示——基础词（the/and 等）与数字本来就不需要释义，
// 曾经显示「未收录」占位，删词典层后基础词均无词条导致满屏噪音
function buildGloss(token) {
    const entry = lookupWord(token);
    if (!entry) {
        return null;
    }
    const span = document.createElement("span");
    if (entry.source === "context") {
        span.className = "gloss ctx";
        span.textContent = `📖 ${conciseTranslation(entry.context || entry.translation)}`;
        return span;
    }
    span.className = "gloss";
    if (entry.source === "official") {
        const tag = document.createElement("span");
        tag.className = "gloss-tag";
        tag.textContent = "官方";
        span.appendChild(tag);
    }
    span.appendChild(document.createTextNode(conciseTranslation(entry.translation || entry.phrase || "")));
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
    letterSpans = []; // 当前词的字母 span 重建（仅当前词有字母级结构）
    // 正在输入的行与带翻译浮层的行需要与上一行拉开距离（给上方浮层留空间）
    const headroomTurns = new Set([turnIndexByWord[currentWordIndex]]);
    if (glossWordIndex >= 0) {
        headroomTurns.add(turnIndexByWord[glossWordIndex]);
    }
    let wordIdx = 0;
    episodeTurns.forEach((turn, ti) => {
        const words = turn.text.split(/\s+/).filter(Boolean);
        const turnStart = wordIdx;
        wordIdx += words.length;
        if (ti < from || ti > to) return;

        const row = document.createElement("div");
        row.className = headroomTurns.has(ti) ? "turn turn-headroom" : "turn";
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
                span.textContent = w;
            } else if (gi === currentWordIndex) {
                // 当前词按字符拆分：键入序列 = 剥离首尾标点后的连续子串（含词内符号），
                // 首尾标点自动通过（不需敲）
                span.className = "current-word";
                let start = 0, end = w.length - 1;
                while (start <= end && !/[a-z0-9]/i.test(w[start])) start++;
                while (end >= start && !/[a-z0-9]/i.test(w[end])) end--;
                for (let ci = 0; ci < w.length; ci++) {
                    const chSpan = document.createElement("span");
                    chSpan.textContent = w[ci];
                    if (ci >= start && ci <= end) {
                        chSpan.dataset.pos = String(ci - start);
                        letterSpans.push(chSpan);
                    } else {
                        chSpan.className = "ltr-punct";
                    }
                    span.appendChild(chSpan);
                }
            } else {
                span.textContent = w;
            }
            para.appendChild(span);
            para.appendChild(document.createTextNode(" "));
            if (gi === glossWordIndex) {
                // 浮层插在宿主词 span 内部：绝对定位显示在词上方，不影响后续文本布局
                const glossEl = buildGloss(w);
                if (glossEl) {
                    span.appendChild(glossEl);
                }
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
    hintCurrent.append(" · 打错原地重打");
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
        currentWordPos = 0;
        glossWordIndex = currentWordIndex > 0 ? currentWordIndex - 1 : -1;

        renderDialog();
        renderHint();
        renderEpisodeMeta(currentEpisode);
        prefetchUpcomingAudio();
        focusCapture();
    } catch (error) {
        console.error("加载节目失败:", error);
        showNotice(`节目加载失败（${error.message}）。可能刚过 30 天下线了，请选择其他节目。`);
        populateEpisodeSelector();
    }
}

// ---------------------------------------------------------------------------
// 逐字母键盘打字（无输入框）：正确字母变绿前进，打错标红原地重打（重打覆盖，无退格）
// ---------------------------------------------------------------------------

let errorAudioCtx = null;
let beepMuted = localStorage.getItem("typelearning:muted") === "1";

function playErrorBeep() {
    // Web Audio 合成短促低音：零资源文件、离线可用；音频不可用/被静音时静默跳过
    if (beepMuted) return;
    try {
        if (!errorAudioCtx) {
            const Ctx = window.AudioContext || window.webkitAudioContext;
            if (!Ctx) return;
            errorAudioCtx = new Ctx();
        }
        if (errorAudioCtx.state === "suspended") {
            errorAudioCtx.resume().catch(() => {});
        }
        const t = errorAudioCtx.currentTime;
        const osc = errorAudioCtx.createOscillator();
        const gain = errorAudioCtx.createGain();
        osc.type = "square";
        osc.frequency.value = 180;
        gain.gain.setValueAtTime(0.06, t);
        gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.09);
        osc.connect(gain).connect(errorAudioCtx.destination);
        osc.start(t);
        osc.stop(t + 0.1);
    } catch (e) { /* 提示音失败不影响打字 */ }
}

function focusCapture() {
    if (kbdCapture) kbdCapture.focus({ preventScroll: true });
}

function completeCurrentWord() {
    const completedWord = originalWords[currentWordIndex];
    currentWordIndex++;
    currentWordPos = 0;
    glossWordIndex = currentWordIndex - 1; // 行内翻译跟随最新完成的词
    renderDialog();
    renderHint();

    speakWord(completedWord);
    prefetchUpcomingAudio();

    if (currentWordIndex >= originalWords.length) {
        glossWordIndex = -1;
        renderDialog();
        writeProgress(currentEpisode.id, currentWordIndex, true);
    } else {
        writeProgress(currentEpisode.id, currentWordIndex);
    }
    renderEpisodeMeta(currentEpisode);
}

function handleLetter(ch) {
    if (!currentEpisode || originalWords.length === 0) return;
    if (currentWordIndex >= originalWords.length) return;
    const target = typingSequence(originalWords[currentWordIndex]);
    if (!target) {
        // 键入序列为空的词元（纯标点等）：直接跳过，不可打
        currentWordIndex++;
        currentWordPos = 0;
        renderDialog();
        renderHint();
        return;
    }
    if (currentWordPos >= target.length) return; // 完词重绘间隙的迟到按键

    if (normalizeChar(ch) === normalizeChar(target[currentWordPos])) {
        const span = letterSpans[currentWordPos];
        if (span) {
            span.classList.remove("letter-bad");
            span.classList.add("letter-ok");
        }
        currentWordPos++;
        if (currentWordPos >= target.length) {
            completeCurrentWord();
        }
    } else {
        const span = letterSpans[currentWordPos];
        if (span) span.classList.add("letter-bad");
        playErrorBeep();
    }
}

document.addEventListener("keydown", (e) => {
    // 剧集选择器聚焦时保留原生按键行为（字母跳选项等）
    if (e.target && e.target.id === "episode-selector") return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === " ") {
        e.preventDefault(); // 防页面滚动
        return;
    }
    // 任意可打印单字符（含词内符号 ' - 等）都参与比对，敲错同样标红+错误音
    if (e.key.length !== 1 || e.key < " " || e.key === "\x7f") return;
    e.preventDefault();
    handleLetter(e.key);
});

// 点击页面任意处重新聚焦捕获输入（手机软键盘）；选择器与链接除外
document.addEventListener("click", (e) => {
    if (e.target.closest("#episode-selector") || e.target.closest("a")) return;
    focusCapture();
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
    // 基础词兜底词典与全局词表（静态资源，一次加载；失败不阻塞主流程）
    try {
        const resp = await fetch("assets/basics.json");
        if (resp.ok) {
            basicsDict = await resp.json();
        }
    } catch (e) {
        console.warn("基础词兜底词典加载失败:", e);
    }
    try {
        const resp = await fetch("data/glossary.json", { cache: "no-cache" });
        if (resp.ok) {
            globalGlossary = await resp.json();
        }
    } catch (e) {
        console.warn("全局词表加载失败:", e);
    }

    // 错误提示音开关（存 localStorage）
    const muteToggle = document.createElement("span");
    muteToggle.id = "mute-toggle";
    muteToggle.title = "打错提醒音开关";
    const renderMuteState = () => {
        muteToggle.textContent = beepMuted ? "🔇 提示音关" : "🔊 提示音";
    };
    renderMuteState();
    muteToggle.addEventListener("click", () => {
        beepMuted = !beepMuted;
        localStorage.setItem("typelearning:muted", beepMuted ? "1" : "0");
        renderMuteState();
    });
    document.querySelector(".app-footer").appendChild(muteToggle);

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
