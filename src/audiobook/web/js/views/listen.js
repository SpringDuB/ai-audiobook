// 手机听书：章节离线包 + 边听边高亮当前句。
//
// 页面结构（移动优先）：顶部章节条 / 中间可滚动的正文 / 底部播放控制。
// 高亮靠 requestAnimationFrame 二分查 SRT 时间轴；滚动跟随可以在用户手动滑动后暂停，
// 用「回到当前句」恢复。播放位置、倍速、下载状态都记在 localStorage。

import { api } from "../api.js";
import { duration as fmtDuration } from "../format.js";
import { icon } from "../icons.js";
import { confirmDialog, emptyState, h, onTeardown, renderWithState, toast } from "../ui.js";
import {
  downloadBook,
  downloadChapter,
  downloadInfo,
  downloadIsCurrent,
  offlineSupported,
  removeBookDownloads,
  storageEstimate,
} from "../offline.js";

const POS_PREFIX = "aiab-listen-pos:";
const LAST_PREFIX = "aiab-listen-last:";
const RATE_KEY = "aiab-listen-rate";
const RATES = [1, 1.25, 1.5, 2, 0.75];
const NARRATOR_NAMES = new Set(["旁白", "narrator", "未知", ""]);

// 预取缓存：听本章时把下一章的时间轴 / 目录拿进内存，切章时不再依赖网络往返。
// 手机上锁屏或切后台时 fetch 会被系统挂起，等网络就是"下一章不自动播"的根源。
// 缓存只活在这次会话里；手动进入章节仍然联网拿最新的，失败才回落到缓存。
const timelineCache = new Map();
const catalogCache = new Map();

function cachedTimeline(bookId, index) {
  const hit = timelineCache.get(`${bookId}:${index}`);
  return hit ? hit.payload : null;
}

function cacheTimeline(bookId, index, payload) {
  timelineCache.set(`${bookId}:${index}`, { at: Date.now(), payload });
  return payload;
}

function cachedCatalog(bookId) {
  const hit = catalogCache.get(bookId);
  return hit ? hit.payload : null;
}

function cacheCatalog(bookId, payload) {
  catalogCache.set(bookId, { at: Date.now(), payload });
  return payload;
}

/** 时间轴：自动续播优先用预取缓存（零网络等待）；手动进入拿最新的，失败再回落缓存。 */
async function loadTimeline(bookId, index, preferCache) {
  if (preferCache) {
    const hit = cachedTimeline(bookId, index);
    if (hit) return hit;
  }
  try {
    return cacheTimeline(bookId, index, await api.subtitles(bookId, index));
  } catch (error) {
    const hit = cachedTimeline(bookId, index);
    if (hit) return hit;
    throw error;
  }
}

async function loadCatalog(bookId, preferCache) {
  if (preferCache) {
    const hit = cachedCatalog(bookId);
    if (hit) return hit;
  }
  try {
    return cacheCatalog(bookId, await api.listenCatalog(bookId));
  } catch (error) {
    const hit = cachedCatalog(bookId);
    if (hit) return hit;
    throw error;
  }
}

// 跨章节复用同一个 <audio>：移动端（尤其 iOS）把"手势解锁"记在媒体元素上，
// 播完自动续播时若换成新元素，play() 会被当成无手势的自动播放拦下来。
let sharedAudio = null;
let audioBound = false;

/* ------------------------------------------------ 常驻播放会话 + 悬浮球

   播放不再属于"播放页"：当前章节 / 时间轴 / 目录活在模块里的 session 上，<audio> 常驻 document。
   路由切换只替换页面 DOM，不再 pause、不再清 src —— 从播放页退回章节列表（或去别的页面）
   声音照常继续，悬浮球负责随时暂停 / 回到播放页。切章时先换源、后渲染画面，避免几秒空档。 */

const session = {
  bookId: null,
  index: null,
  key: null,
  // <audio> 里实际装载的章节：切章时 key 会先变成目标章，src 还是旧章，
  // 进度落盘 / 高亮都必须认 srcKey，不能认 key
  srcBookId: null,
  srcIndex: null,
  srcKey: null,
  timeline: null,
  catalog: null,
  cues: [],
  prev: null,
  next: null,
  loaded: false, // <audio> 里装载的就是这一章
  buffering: false, // 转码 / 等第一段数据中
  switching: false, // 正在换 src：这期间的 pause 是换源的副作用，不能拿去写进度
  resume: false, // 下一次 loadedmetadata 是否跳到上次听到的位置
  blocked: false, // 自动播放被浏览器拦下，等下一次点击补播
  switch: null, // 正在进行的切章 Promise（同一章并发进入时复用）
};

let switchSeq = 0;
let mini = null;
// 播放页版本号：播放页 → 播放页时用它作废上一页"延迟收起播放页样式"的回调，避免顶栏/底栏闪一下
let pageToken = 0;

function m4aUrl(bookId, index) {
  return `/api/books/${bookId}/chapters/${index}/audio.m4a`;
}

function audioElement() {
  if (!sharedAudio) {
    sharedAudio = new Audio();
    sharedAudio.className = "listen__audio";
    sharedAudio.preload = "metadata";
  }
  // 页面切换时 renderWithState 会替换整棵 DOM；<audio> 固定挂在 document 上，
  // 跨章节只换 src，元素本身不动（被摘掉的话 play() 会被浏览器拒绝）。
  if (!sharedAudio.isConnected) document.body.append(sharedAudio);
  bindAudio();
  return sharedAudio;
}

function saveCurrent() {
  if (!session.srcKey || !session.loaded || !sharedAudio) return;
  savePosition(session.srcBookId, session.srcIndex, sharedAudio.currentTime || 0, sharedAudio.playbackRate);
}

async function playAudio() {
  const audio = audioElement();
  try {
    await audio.play();
  } catch (error) {
    // 切章时上一次 play() 会被新 load 打断，这是正常的，不用提示
    if (error?.name !== "AbortError") {
      session.blocked = true;
      toast("浏览器拦住了自动播放，点一下播放键继续", "error");
    }
  }
}

function toggleAudio() {
  const audio = audioElement();
  if (audio.paused) void playAudio();
  else audio.pause();
}

/** 彻底停下：清会话、清 src、收起悬浮球（悬浮球上的 × / 锁屏 stop 用）。 */
function stopSession() {
  const audio = audioElement();
  saveCurrent();
  audio.pause();
  audio.removeAttribute("src");
  audio.load();
  switchSeq += 1;
  session.bookId = null;
  session.index = null;
  session.key = null;
  session.srcBookId = null;
  session.srcIndex = null;
  session.srcKey = null;
  session.timeline = null;
  session.catalog = null;
  session.cues = [];
  session.prev = null;
  session.next = null;
  session.loaded = false;
  session.buffering = false;
  session.switching = false;
  session.blocked = false;
  session.switch = null;
  refreshMediaSession();
  paintMini();
}

function syncMediaPosition() {
  if (!("mediaSession" in navigator) || !session.key || !sharedAudio) return;
  const audio = sharedAudio;
  if (!Number.isFinite(audio.duration) || audio.duration <= 0) return;
  try {
    navigator.mediaSession.setPositionState({
      duration: audio.duration,
      position: Math.min(audio.currentTime || 0, audio.duration),
      playbackRate: audio.playbackRate || 1,
    });
  } catch (error) {
    /* 部分浏览器不支持 setPositionState */
  }
}

function refreshMediaSession() {
  if (!("mediaSession" in navigator)) return;
  if (!session.key) {
    try {
      navigator.mediaSession.metadata = null;
      navigator.mediaSession.playbackState = "none";
    } catch (error) {
      /* 忽略 */
    }
    return;
  }
  const timeline = session.timeline || {};
  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: timeline.title || `第 ${session.index} 章`,
      artist: timeline.book_title || "",
      album: "AI 有声书",
      artwork: [{ src: "/static/icons/icon-512.png", sizes: "512x512", type: "image/png" }],
    });
  } catch (error) {
    /* 忽略 */
  }
  syncMediaPosition();
}

function bindMediaSession() {
  if (!("mediaSession" in navigator)) return;
  const set = (name, handler) => {
    try {
      navigator.mediaSession.setActionHandler(name, handler);
    } catch (error) {
      /* iOS 上部分 handler 不支持 */
    }
  };
  set("play", () => void playAudio());
  set("pause", () => sharedAudio?.pause());
  set("stop", () => stopSession());
  set("seekbackward", () => {
    if (sharedAudio) sharedAudio.currentTime = Math.max(0, (sharedAudio.currentTime || 0) - 15);
  });
  set("seekforward", () => {
    if (sharedAudio) sharedAudio.currentTime = Math.min(sharedAudio.duration || 0, (sharedAudio.currentTime || 0) + 15);
  });
  set("previoustrack", () => {
    if (session.prev) goChapter(session.bookId, session.prev.index, { autoplay: !sharedAudio?.paused, resume: true });
  });
  set("nexttrack", () => {
    if (session.next) goChapter(session.bookId, session.next.index, { autoplay: !sharedAudio?.paused, resume: true });
  });
}

/** 常驻 <audio> 监听：只绑一次，切页不断。 */
function bindAudio() {
  if (audioBound) return;
  audioBound = true;
  const audio = sharedAudio;
  let lastSaved = 0;
  let lastPos = 0;

  audio.addEventListener("play", () => {
    session.blocked = false;
    if ("mediaSession" in navigator) {
      try {
        navigator.mediaSession.playbackState = "playing";
      } catch (error) {
        /* 忽略 */
      }
    }
    paintMini();
  });
  audio.addEventListener("pause", () => {
    if ("mediaSession" in navigator) {
      try {
        navigator.mediaSession.playbackState = session.key ? "paused" : "none";
      } catch (error) {
        /* 忽略 */
      }
    }
    // 换 src 时浏览器也会补一个 pause：那是切章副作用，别拿 0 秒去覆盖进度
    if (!session.switching) saveCurrent();
    paintMini();
  });
  audio.addEventListener("playing", () => {
    session.switching = false;
    session.buffering = false;
    paintMini();
  });
  audio.addEventListener("canplay", () => {
    session.switching = false;
    session.buffering = false;
    paintMini();
  });
  audio.addEventListener("waiting", () => {
    if (!session.key) return;
    session.buffering = true;
    paintMini();
  });
  audio.addEventListener("timeupdate", () => {
    const now = Date.now();
    // 进度条每次 timeupdate 都画；落 localStorage / 更新锁屏进度节流
    paintMini();
    if (now - lastSaved > 5000) {
      lastSaved = now;
      if (!session.switching) saveCurrent();
    }
    if (now - lastPos > 2000) {
      lastPos = now;
      syncMediaPosition();
    }
  });
  audio.addEventListener("durationchange", () => {
    paintMini();
    refreshMediaSession();
  });
  audio.addEventListener("loadedmetadata", () => {
    const rate = readRate();
    audio.defaultPlaybackRate = rate;
    audio.playbackRate = rate;
    if (session.resume && session.loaded) {
      const saved = readPosition(session.bookId, session.index);
      const total = audio.duration || session.timeline?.duration || 0;
      if (saved > 5 && (!total || saved < total - 3)) {
        audio.currentTime = saved;
        session.resumedAt = saved;
      }
    }
    session.resume = false;
    session.switching = false;
    session.buffering = false;
    paintMini();
    refreshMediaSession();
  });
  audio.addEventListener("ended", () => {
    if (!session.key) return;
    savePosition(session.bookId, session.index, 0, audio.playbackRate);
    const next = session.next;
    if (!next) {
      toast("已经是最后一章，播放结束");
      paintMini();
      return;
    }
    toast(`第 ${session.index} 章播完，继续第 ${next.index} 章`);
    goChapter(session.bookId, next.index, { autoplay: true, resume: false });
  });
  audio.addEventListener("error", () => {
    if (!session.key || session.blocked) return;
    session.switching = false;
    session.buffering = false;
    paintMini();
    toast("音频加载失败：检查网络，或重新下载这一章", "error");
  });
}

/** 悬浮球：任何页面（除播放页）都能暂停 / 回到播放页 / 停止。 */
function ensureMini() {
  if (mini) return mini;
  const bar = h("i", { class: "listen__miniBar" });
  const ball = h("button", { class: "listen__miniBall", type: "button", title: "暂停 / 继续" });
  const title = h("strong", { class: "listen__miniTitle" }, "正在播放");
  const meta = h("span", { class: "listen__miniMeta mono" }, "");
  const open = h(
    "button",
    { class: "listen__miniOpen", type: "button", title: "回到播放页" },
    h("span", { class: "listen__miniText" }, title, meta),
  );
  const nextButton = h("button", { class: "listen__miniBtn", type: "button", title: "下一章" }, icon("skip-forward", { size: 16 }));
  const closeButton = h("button", { class: "listen__miniBtn", type: "button", title: "停止播放" }, icon("x", { size: 16 }));
  const root = h(
    "div",
    { class: "listen__mini", role: "region", "aria-label": "正在播放", "aria-hidden": "true" },
    h("span", { class: "listen__miniTrack" }, bar),
    ball,
    open,
    nextButton,
    closeButton,
  );
  ball.addEventListener("click", () => toggleAudio());
  open.addEventListener("click", () => {
    if (!session.bookId) return;
    window.location.hash = `#/listen/${session.bookId}/${session.index}`;
  });
  nextButton.addEventListener("click", () => {
    if (!session.next) return;
    goChapter(session.bookId, session.next.index, { autoplay: true, resume: true });
  });
  closeButton.addEventListener("click", () => {
    stopSession();
    toast("已停止播放");
  });
  document.body.append(root);
  mini = { root, bar, ball, title, meta, open, nextButton };
  return mini;
}

function paintMini() {
  const view = ensureMini();
  const active = Boolean(session.key && session.bookId);
  // 播放页自己有完整控制条，悬浮球只在别的页面出现
  const visible = active && !document.body.classList.contains("is-listening");
  view.root.classList.toggle("is-open", visible);
  view.root.setAttribute("aria-hidden", visible ? "false" : "true");
  // 有悬浮球时给正文留出底部空间，避免挡住最后一两行
  document.body.classList.toggle("has-mini", visible);
  if (!visible || !sharedAudio) return;

  const timeline = session.timeline || {};
  const audio = sharedAudio;
  const total = Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : timeline.duration || 0;
  const position = audio.currentTime || 0;
  view.title.textContent = timeline.title || `第 ${session.index} 章`;
  const book = timeline.book_title || session.catalog?.title || "";
  view.meta.textContent = session.buffering
    ? "准备中…"
    : `${book ? `${book} · ` : ""}${clock(position)} / ${clock(total)}`;
  const state = session.buffering ? "loading" : audio.paused ? "paused" : "playing";
  view.ball.dataset.state = state;
  view.ball.replaceChildren(icon(session.buffering ? "hourglass" : audio.paused ? "play" : "pause", { size: 18 }));
  view.ball.setAttribute("aria-label", audio.paused ? "继续播放" : "暂停播放");
  view.nextButton.hidden = !session.next;
  view.bar.style.transform = `scaleX(${total ? Math.min(1, position / total) : 0})`;
}

/** session 上重新算上一章 / 下一章。 */
function refreshNeighbors() {
  const chapters = session.catalog?.chapters || [];
  const position = chapters.findIndex((chapter) => chapter.index === session.index);
  session.prev = position > 0 ? chapters[position - 1] : null;
  session.next = position >= 0 && position < chapters.length - 1 ? chapters[position + 1] : null;
}

/** 换源：只改 <audio> 的 src，不动页面 DOM。 */
function applySource(bookId, index, mtime) {
  const audio = audioElement();
  const rate = readRate();
  audio.defaultPlaybackRate = rate;
  audio.playbackRate = rate;
  const url = new URL(`${m4aUrl(bookId, index)}${mtime ? `?v=${mtime}` : ""}`, window.location.href).href;
  if (audio.src === url) return;
  // 换源会连带补一个 pause 事件：这期间到 loadedmetadata 之前不写进度
  session.switching = true;
  audio.src = url;
  session.srcBookId = bookId;
  session.srcIndex = index;
  session.srcKey = `${bookId}:${index}`;
}

// 下一章音频预取：用第二个（静音）媒体元素把字节拉进浏览器缓存，
// 续播时换 src 几乎立刻有数据；离线包已下载过的章节不用重复拉。
let preloadElement = null;
function preloadAudio(bookId, index, mtime) {
  if (!mtime) return;
  const url = `${m4aUrl(bookId, index)}?v=${mtime}`;
  try {
    if (!preloadElement) {
      preloadElement = new Audio();
      preloadElement.preload = "auto";
      preloadElement.muted = true;
    }
    if (preloadElement.dataset.key === url) return;
    preloadElement.dataset.key = url;
    preloadElement.src = url;
    preloadElement.load();
  } catch (error) {
    /* 预取失败无所谓，正常切章还会再取一次 */
  }
}

/**
 * 打开 / 切入一章：任何入口（章节列表、上/下一章、播完续播、悬浮球）都走这里。
 * 先换源（能同步就同步，手势里调用时能马上出声），再拉时间轴 / 目录；同一章的并发调用复用同一个 Promise。
 */
async function openChapter(bookId, index, { autoplay = null, resume = true, timeline = null, catalog = null } = {}) {
  const audio = audioElement();
  const key = `${bookId}:${index}`;

  if (session.key === key && session.switch) return session.switch;
  if (session.key === key && session.loaded) {
    // 已经在放这一章（典型：悬浮球 / 返回播放页）：音频一个字都不动，直接复用会话数据
    if (timeline) session.timeline = timeline;
    if (catalog) {
      session.catalog = catalog;
      cacheCatalog(bookId, catalog);
      refreshNeighbors();
    }
    if (audio.ended) audio.currentTime = 0;
    if (autoplay === true) void playAudio();
    paintMini();
    return session;
  }

  // 换章前先把这一章的进度落盘：src 一换 currentTime 就归零了
  saveCurrent();
  const sameBook = session.bookId === bookId;
  const keepPlaying = session.loaded && !audio.paused && !audio.ended && Boolean(audio.src);
  const wantPlay = autoplay === null ? keepPlaying : Boolean(autoplay);
  const seq = ++switchSeq;

  session.bookId = bookId;
  session.index = index;
  session.key = key;
  session.loaded = false;
  session.buffering = false;
  session.blocked = false;
  session.resume = resume;
  session.timeline = timeline || cachedTimeline(bookId, index) || null;
  session.cues = session.timeline?.cues || [];
  session.catalog = catalog || (sameBook ? session.catalog : cachedCatalog(bookId)) || null;
  if (catalog) cacheCatalog(bookId, catalog);
  refreshNeighbors();
  paintMini();

  const task = (async () => {
    // 1) 出声音优先：目录里已转码就直接上源；否则让服务端转码（期间悬浮球显示"准备中"）
    const row = (session.catalog?.chapters || []).find((chapter) => chapter.index === index) || null;
    let mtime = row?.m4a_ready ? row.m4a_mtime : null;
    if (mtime) {
      applySource(bookId, index, mtime);
    } else {
      session.buffering = true;
      paintMini();
      try {
        const info = await api.prepareMobile(bookId, index, {});
        if (seq !== switchSeq) return session;
        mtime = info.mtime;
        if (row) {
          row.m4a_ready = true;
          row.m4a_mtime = info.mtime;
          row.m4a_bytes = info.bytes;
        }
        if (session.catalog) cacheCatalog(bookId, session.catalog);
        applySource(bookId, index, mtime);
      } catch (error) {
        if (seq !== switchSeq) return session;
        // 离线 / 转码失败：仍然上源，交给 service worker 或原始地址兜底
        applySource(bookId, index, row?.m4a_mtime || null);
      }
    }
    if (seq !== switchSeq) return session;
    session.buffering = false;
    session.loaded = true;
    if (wantPlay) void playAudio();
    paintMini();

    // 2) 画面数据：缓存优先，失败也不影响已经响起来的声音
    try {
      const [tl, cat] = await Promise.all([
        session.timeline || loadTimeline(bookId, index, false),
        session.catalog || loadCatalog(bookId, false).catch(() => null),
      ]);
      if (seq !== switchSeq) return session;
      session.timeline = tl;
      session.cues = tl?.cues || [];
      if (cat) {
        session.catalog = cat;
        cacheCatalog(bookId, cat);
      }
      refreshNeighbors();
      paintMini();
      refreshMediaSession();
    } catch (error) {
      if (seq !== switchSeq) return session;
      paintMini();
    }
    return session;
  })();

  session.switch = task;
  try {
    return await task;
  } finally {
    if (seq === switchSeq) session.switch = null;
  }
}

/** 切章 + 改地址栏。先切源再导航，播放页随后直接接管（不会再 pause / 重新加载）。 */
function goChapter(bookId, index, { autoplay = true, resume = false } = {}) {
  if (!bookId) return;
  void openChapter(bookId, index, { autoplay, resume });
  const hash = `#/listen/${bookId}/${index}`;
  if (window.location.hash !== hash) window.location.hash = hash;
}

/** 手机锁屏 / 切后台被系统暂停后，回到前台点击任意位置补播。 */
document.addEventListener("pointerdown", () => {
  if (session.blocked) {
    session.blocked = false;
    void playAudio();
  }
});

// 锁屏 / 切后台期间被系统挡下的续播，回到前台时补一次
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState !== "visible" || !session.blocked) return;
  session.blocked = false;
  void playAudio();
});

bindMediaSession();

export function render(host, route) {
  const isPlayer =
    route.name === "listen" && Boolean(route.bookId) && route.index !== null && route.index !== undefined;
  if (!isPlayer) {
    // 离开播放页只是收起播放 UI：声音照常，悬浮球接管
    paintMini();
  }
  return renderWithState(host, () => {
    if (!route.bookId) return buildPicker();
    if (route.index === null || route.index === undefined) return buildCatalog(host, route.bookId);
    return buildPlayer(host, route.bookId, Number(route.index));
  });
}

/* ------------------------------------------------------------------ 工具 */

function clock(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  const mm = hours ? String(minutes).padStart(2, "0") : String(minutes);
  return hours ? `${hours}:${mm}:${String(secs).padStart(2, "0")}` : `${mm}:${String(secs).padStart(2, "0")}`;
}

function bytes(size) {
  const value = Number(size) || 0;
  if (!value) return "";
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)} GB`;
  return `${(value / 1e6).toFixed(value >= 1e7 ? 0 : 1)} MB`;
}

function hueFor(name) {
  let hash = 0;
  for (const char of String(name || "")) hash = (hash * 31 + char.codePointAt(0)) % 997;
  return (hash % 8) + 1;
}

function savePosition(bookId, index, seconds, rate) {
  const seconds_ = Math.max(0, Number(seconds) || 0);
  try {
    localStorage.setItem(`${POS_PREFIX}${bookId}:${index}`, String(seconds_));
    const previous = readLast(bookId);
    const rate_ = Number(rate) > 0 ? Number(rate) : previous?.rate || 1;
    localStorage.setItem(
      `${LAST_PREFIX}${bookId}`,
      JSON.stringify({ index, seconds: seconds_, rate: rate_, at: Date.now() }),
    );
  } catch (error) {
    /* 隐私模式：只是记不住进度 */
  }
}

function readPosition(bookId, index) {
  try {
    return Number(localStorage.getItem(`${POS_PREFIX}${bookId}:${index}`)) || 0;
  } catch (error) {
    return 0;
  }
}

function readLast(bookId) {
  try {
    const raw = JSON.parse(localStorage.getItem(`${LAST_PREFIX}${bookId}`) || "null");
    return raw && typeof raw === "object" ? raw : null;
  } catch (error) {
    return null;
  }
}

function readRate() {
  try {
    const value = Number(localStorage.getItem(RATE_KEY));
    return RATES.includes(value) ? value : 1;
  } catch (error) {
    return 1;
  }
}

function writeRate(value) {
  try {
    localStorage.setItem(RATE_KEY, String(value));
  } catch (error) {
    /* 无所谓 */
  }
}

/** 只更新"这本书上次听到"记录里的倍速，不动已存的位置。 */
function rememberRate(bookId, rate) {
  try {
    const last = readLast(bookId);
    if (!last) return;
    localStorage.setItem(`${LAST_PREFIX}${bookId}`, JSON.stringify({ ...last, rate, at: Date.now() }));
  } catch (error) {
    /* 无所谓 */
  }
}

function offlineBanner() {
  if (offlineSupported()) return null;
  return h(
    "p",
    { class: "listen__notice" },
    icon("wifi-off", { size: 14 }),
    "当前是 HTTP 局域网访问：可以边下边听，但不能保存离线包。要用离线听书，请通过 HTTPS（Tailscale / 隧道）或 localhost 打开。",
  );
}

function pageHead(title, hint, actions = []) {
  return h(
    "header",
    { class: "page-head listen__head" },
    h("div", { class: "page-head__title" }, h("h1", { class: "letterpress" }, title), hint ? h("p", { class: "muted" }, hint) : null),
    h("div", { class: "page-head__actions" }, ...actions),
  );
}

/* ------------------------------------------------------------------ 书 → 听书目录 */

async function buildPicker() {
  const payload = await api.books();
  const books = (payload.books || []).filter((book) => Number(book.stats?.generated || 0) > 0);
  const head = pageHead("听书", "章节下载到手机后离线可听；播放时正文会跟着声音逐句高亮。");
  if (!books.length) {
    return h(
      "div",
      { class: "listen listen--picker" },
      head,
      offlineBanner(),
      emptyState("还没有可听的书", "先在书架里生成章节音频，再回来这里。"),
    );
  }
  const cards = books.map((book) => {
    const stats = book.stats || {};
    const last = readLast(book.id);
    const suffix = last
      ? ` · 上次听到第 ${last.index} 章 ${clock(last.seconds)}${last.rate ? ` · ${last.rate}×` : ""}`
      : "";
    return h(
      "a",
      { class: "sheet listen__book", href: `#/listen/${book.id}` },
      h("div", { class: "listen__bookTop" }, h("strong", { class: "letterpress" }, book.title || book.id)),
      h(
        "p",
        { class: "mono muted" },
        `已生成 ${stats.generated}/${stats.chapters} 章${stats.duration_sec ? ` · ${fmtDuration(stats.duration_sec)}` : ""}${suffix}`,
      ),
      h("span", { class: "listen__bookGo" }, icon("chevron-right", { size: 16 })),
    );
  });
  return h("div", { class: "listen listen--picker" }, head, offlineBanner(), h("div", { class: "listen__books" }, ...cards));
}

/* ------------------------------------------------------------------ 章节列表 */

async function buildCatalog(host, bookId) {
  const catalog = await api.listenCatalog(bookId);
  host.dataset.listen = "catalog";
  onTeardown(() => {
    delete host.dataset.listen;
  });

  const chapters = catalog.chapters || [];
  const usage = h("span", { class: "listen__usage mono muted" }, "");
  const list = h("div", { class: "listen__chapters" });

  const downloaded = () => chapters.filter((chapter) => downloadIsCurrent(bookId, chapter)).length;
  const paintUsage = async () => {
    const estimate = await storageEstimate();
    const quota = estimate?.quota ? ` / 可用 ${bytes(estimate.quota)}` : "";
    usage.textContent = `已离线 ${downloaded()}/${chapters.length} 章${quota}`;
  };

  const rows = chapters.map((chapter) => chapterRow(bookId, chapter, () => {
    paintUsage();
  }));
  list.replaceChildren(...rows);
  // 正在播放的那一行标出来，用户回到列表能一眼看到"刚才听到哪"
  const markPlaying = () => {
    for (const row of rows) {
      row.classList.toggle("is-playing", session.bookId === bookId && session.index === Number(row.dataset.index));
    }
  };
  markPlaying();
  // 播完自动续章时列表不用刷新也能跟上
  const markTimer = window.setInterval(markPlaying, 2000);
  onTeardown(() => window.clearInterval(markTimer));

  const last = readLast(bookId);
  const continueRow =
    last && chapters.some((chapter) => chapter.index === last.index)
      ? h(
          "a",
          { class: "listen__resume", href: `#/listen/${bookId}/${last.index}` },
          icon("play", { size: 14 }),
          `继续听 第 ${last.index} 章 · ${clock(last.seconds)}${last.rate ? ` · ${last.rate}×` : ""}`,
        )
      : null;

  const downloadAll = h(
    "button",
    {
      class: "btn",
      type: "button",
      title: "把这一本还没下载的章节依次下载到手机",
      onClick: async () => {
        if (!offlineSupported()) {
          toast("离线下载需要 HTTPS 访问（局域网 http 只能在线听）", "error");
          return;
        }
        downloadAll.disabled = true;
        try {
          const result = await downloadBook(bookId, chapters, {
            force: false,
            onProgress: ({ done, total, phase, loaded, total: bytes_ }) => {
              if (phase === "prepare") downloadAll.textContent = `转码中 ${done + 1}/${total}…`;
              else if (phase === "download" && bytes_) downloadAll.textContent = `下载 ${done + 1}/${total} · ${Math.round((loaded / bytes_) * 100)}%`;
            },
          });
          toast(result.total ? `已下载 ${result.done} 章` : "全部章节都已是最新");
        } catch (error) {
          toast(error.message, "error");
        } finally {
          downloadAll.disabled = false;
          downloadAll.replaceChildren(icon("download", { size: 13 }), "下载整本");
          rows.forEach((row) => row.paint?.());
          paintUsage();
        }
      },
    },
    icon("download", { size: 13 }),
    "下载整本",
  );

  const clearAll = h(
    "button",
    {
      class: "btn btn-ghost",
      type: "button",
      title: "删掉已下载到手机的章节音频（服务器上的成品不受影响）",
      onClick: async () => {
        const ok = await confirmDialog({
          title: "清空本机离线包？",
          message: `会删掉《${catalog.title}》已经下载到这台设备的章节音频，服务器上的成品和字幕不受影响。`,
          confirmLabel: "清空",
          danger: true,
        });
        if (!ok) return;
        await removeBookDownloads(bookId, chapters);
        toast("已清空离线包");
        rows.forEach((row) => row.paint?.());
        paintUsage();
        paintClear();
      },
    },
    icon("trash-2", { size: 13 }),
    "清空离线",
  );
  const paintClear = () => {
    clearAll.style.display = downloaded() ? "" : "none";
  };

  paintUsage();
  paintClear();
  const back = h("a", { class: "btn btn-ghost", href: "#/listen" }, icon("chevron-left", { size: 13 }), "全部书");
  return h(
    "div",
    { class: "listen listen--catalog" },
    pageHead(catalog.title, "点章节开听；右侧按钮把这一章存到手机上，之后没网也能听。", [back, usage, downloadAll, clearAll]),
    offlineBanner(),
    continueRow,
    list,
  );
}

function chapterRow(bookId, chapter, onChange) {
  const status = h("span", { class: "listen__rowState" });
  const button = h("button", { class: "listen__rowDl", type: "button" }, status);
  const paint = () => {
    const info = downloadInfo(bookId, chapter.index);
    if (downloadIsCurrent(bookId, chapter)) {
      status.dataset.state = "done";
      status.replaceChildren(icon("circle-check", { size: 13 }), "已下载");
      button.title = "已下载到本机；点击重新下载";
    } else if (info) {
      status.dataset.state = "stale";
      status.replaceChildren(icon("download", { size: 13 }), "有更新");
      button.title = "服务器上的音频更新过，点一下重新下载";
    } else {
      status.dataset.state = "idle";
      status.replaceChildren(icon("download", { size: 13 }), bytes(chapter.estimated_bytes));
      button.title = `下载到手机（约 ${bytes(chapter.estimated_bytes)}）`;
    }
  };
  button.addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (!offlineSupported()) {
      toast("离线下载需要 HTTPS 访问（局域网 http 只能在线听）", "error");
      return;
    }
    if (downloadIsCurrent(bookId, chapter)) {
      const again = await confirmDialog({
        title: "重新下载这一章？",
        message: `${chapter.title || `第 ${chapter.index} 章`} 已经下载过了；重新下载会覆盖手机里的离线包。`,
        confirmLabel: "重新下载",
      });
      if (!again) return;
    }
    button.disabled = true;
    try {
      await downloadChapter(bookId, chapter, {
        force: true,
        onProgress: ({ phase, loaded, total }) => {
          if (phase === "prepare") status.replaceChildren(icon("hourglass", { size: 13 }), "转码中…");
          else if (phase === "download") {
            const percent = total ? Math.round((loaded / total) * 100) : 0;
            status.replaceChildren(icon("download", { size: 13 }), `${percent}%`);
          }
        },
      });
      toast(`第 ${chapter.index} 章已下载，可离线收听`);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      button.disabled = false;
      paint();
      onChange?.();
    }
  });

  paint();
  const row = h(
    "div",
    { class: "listen__row" },
    h(
      "a",
      { class: "listen__rowMain", href: `#/listen/${bookId}/${chapter.index}` },
      h("span", { class: "listen__rowIndex mono" }, String(chapter.index).padStart(3, "0")),
    h(
      "span",
      { class: "listen__rowText" },
      h(
        "span",
        { class: "listen__rowName" },
        h("strong", {}, chapter.title || `第 ${chapter.index} 章`),
        h("span", { class: "listen__rowBadge" }, icon("audio-lines", { size: 11 }), "播放中"),
      ),
      h("span", { class: "mono muted" }, `${clock(chapter.duration)} · ${bytes(chapter.estimated_bytes)} · ${chapter.cues} 句`),
    ),
  ),
  button,
);
row.dataset.index = String(chapter.index);
row.paint = paint;
return row;
}

/* ------------------------------------------------------------------ 播放页 */

async function buildPlayer(host, bookId, index) {
  // 播放页只负责"画"：音频、切章、进度都在常驻 session 上（见文件顶部）。
  // 同一章（从悬浮球回来 / 地址没变）零网络等待：直接复用内存里的时间轴，且完全不碰 <audio>。
  pageToken += 1;
  document.body.classList.add("is-listening");
  paintMini();
  let info;
  try {
    info = await openChapter(bookId, index, { autoplay: null });
  } catch (error) {
    document.body.classList.remove("is-listening");
    throw error;
  }
  if (!info.timeline) {
    document.body.classList.remove("is-listening");
    throw new Error("这一章还没有可听的成品音频（先在本章生成有声书，再回来听）");
  }
  host.dataset.listen = "player";
  const audio = audioElement();
  const timeline = info.timeline;
  const catalog = info.catalog;

  const chapterList = catalog?.chapters || [];
  const position = chapterList.findIndex((chapter) => chapter.index === index);
  const prev = position > 0 ? chapterList[position - 1] : null;
  const next = position >= 0 && position < chapterList.length - 1 ? chapterList[position + 1] : null;
  const cues = timeline.cues || [];
  const estimatedBytes = Math.round(((timeline.duration || 0) * (timeline.audio.bitrate_kbps || 64) * 1000) / 8);

  // 换源（切章 / 重新加载）时浏览器会把 playbackRate 重置回 defaultPlaybackRate，
  // 两个都要写，倍速才会跟着章节走。
  const rememberedRate = readRate();
  audio.defaultPlaybackRate = rememberedRate;
  audio.playbackRate = rememberedRate;

  const seek = h("input", { class: "listen__seek", type: "range", min: "0", max: String(timeline.duration || 1), step: "0.1", value: "0", "aria-label": "播放进度" });
  const elapsed = h("span", { class: "mono" }, "0:00");
  const remain = h("span", { class: "mono" }, clock(timeline.duration));
  const playButton = h("button", { class: "listen__play", type: "button", "aria-label": "播放" }, icon("play", { size: 26 }));
  const rateButton = h("button", { class: "listen__tool", type: "button", title: "切换倍速" }, `${audio.playbackRate}×`);
  const timerButton = h("button", { class: "listen__tool", type: "button", title: "定时停止播放（睡眠定时）" }, "定时");
  const listButton = h("button", { class: "listen__tool", type: "button", title: "章节列表" }, icon("list-music", { size: 16 }));
  const downloadButton = h("button", { class: "listen__tool", type: "button" });
  const status = h("span", { class: "listen__status mono muted" }, "");

  const lines = cues.map((cue, i) => {
    const label = NARRATOR_NAMES.has(cue.speaker_name || "") ? null : h(
      "span",
      { class: "listen__who", dataset: { hue: String(hueFor(cue.speaker_name || cue.speaker || i)) } },
      cue.speaker_name,
    );
    return h(
      "p",
      {
        class: "listen__line",
        dataset: { i: String(i) },
        onClick: () => {
          audio.currentTime = cue.start + 0.01;
          follow = true;
          if (audio.paused) void playAudio();
        },
      },
      label,
      h("span", { class: "listen__text" }, cue.text),
    );
  });

  const stage = h(
    "div",
    { class: "listen__stage" },
    h(
      "article",
      { class: "listen__sheet" },
      h("p", { class: "listen__kicker mono" }, timeline.book_title || ""),
      h("h1", { class: "letterpress listen__chapter" }, timeline.title || `第 ${index} 章`),
      ...lines,
      h("p", { class: "listen__finish muted" }, "—— 本章完 ——"),
    ),
  );

  const followButton = h(
    "button",
    { class: "listen__follow", type: "button", hidden: true, onClick: () => { follow = true; followButton.hidden = true; scrollToActive(true); } },
    "回到当前句",
  );

  const sleep = { minutes: 0, endsAt: 0, ticker: 0 };
  const prevButton = h(
    "button",
    {
      class: "listen__tool",
      type: "button",
      title: prev ? `上一章：${prev.title}` : "已经是第一章",
      disabled: !prev,
      onClick: () => prev && goChapter(bookId, prev.index, { autoplay: !audio.paused, resume: true }),
    },
    icon("skip-back", { size: 18 }),
  );
  const nextButton = h(
    "button",
    {
      class: "listen__tool",
      type: "button",
      title: next ? `下一章：${next.title}` : "已经是最后一章",
      disabled: !next,
      onClick: () => {
        if (!next) return;
        goChapter(bookId, next.index, { autoplay: !audio.paused, resume: true });
      },
    },
    icon("skip-forward", { size: 18 }),
  );
  const backButton = h("a", { class: "icon-btn", href: `#/listen/${bookId}`, title: "章节列表" }, icon("chevron-left", { size: 16 }));

  const bar = h(
    "header",
    { class: "listen__bar" },
    backButton,
    h(
      "div",
      { class: "listen__barTitle" },
      h("strong", {}, timeline.title || `第 ${index} 章`),
      h("span", { class: "mono" }, status),
    ),
    downloadButton,
  );

  const controls = h(
    "footer",
    { class: "listen__controls" },
    h("div", { class: "listen__track" }, seek),
    h("div", { class: "listen__times" }, elapsed, remain),
    h(
      "div",
      { class: "listen__buttons" },
      prevButton,
      h("button", { class: "listen__tool", type: "button", title: "后退 15 秒", onClick: () => { audio.currentTime = Math.max(0, audio.currentTime - 15); } }, icon("rotate-ccw", { size: 18 })),
      playButton,
      h("button", { class: "listen__tool", type: "button", title: "前进 15 秒", onClick: () => { audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 15); } }, icon("rotate-cw", { size: 18 })),
      nextButton,
    ),
    h("div", { class: "listen__tools" }, rateButton, timerButton, listButton),
  );

  const root = h("div", { class: "listen listen--player" }, bar, stage, followButton, controls);

  /* --- 播放与高亮 --- */

  let follow = true;
  let activeIndex = -1;
  let autoScrollAt = 0;
  let raf = 0;
  let dragging = false;

  const cueIndexAt = (time) => {
    let low = 0;
    let high = cues.length - 1;
    let found = -1;
    while (low <= high) {
      const mid = (low + high) >> 1;
      if (cues[mid].start <= time) {
        found = mid;
        low = mid + 1;
      } else {
        high = mid - 1;
      }
    }
    return found;
  };

  const scrollToActive = (force = false) => {
    const node = lines[activeIndex];
    if (!node) return;
    const box = stage.getBoundingClientRect();
    const rect = node.getBoundingClientRect();
    const center = box.top + box.height * 0.42;
    const inZone = rect.top > box.top + box.height * 0.12 && rect.bottom < box.bottom - box.height * 0.18;
    if (!force && inZone) return;
    if (!force && Date.now() - autoScrollAt < 1200) return;
    autoScrollAt = Date.now();
    node.scrollIntoView({ block: "center", behavior: force ? "auto" : "smooth" });
  };

  const paintActive = (index_) => {
    if (index_ === activeIndex) return;
    if (activeIndex >= 0) lines[activeIndex]?.classList.remove("is-active");
    activeIndex = index_;
    if (activeIndex >= 0) {
      lines[activeIndex]?.classList.add("is-active");
      if (follow) scrollToActive();
    }
  };

  const tick = () => {
    raf = requestAnimationFrame(tick);
    if (!cues.length) return;
    // 切章时 src 可能还没换过来（旧章还在响）：这时不高亮新章的句子
    if (!session.loaded || session.srcKey !== `${bookId}:${index}`) return;
    // 高亮由播放页负责；进度落盘归常驻 session（切页/后台也不丢）
    paintActive(cueIndexAt(audio.currentTime || 0));
  };

  const syncProgress = () => {
    if (dragging) return;
    const value = audio.currentTime || 0;
    seek.value = String(value);
    elapsed.textContent = clock(value);
    const total = Number.isFinite(audio.duration) ? audio.duration : timeline.duration || 0;
    remain.textContent = `-${clock(Math.max(0, total - value))}`;
    if ("mediaSession" in navigator && Number.isFinite(audio.duration) && audio.duration > 0) {
      try {
        navigator.mediaSession.setPositionState({ duration: audio.duration, position: Math.min(value, audio.duration), playbackRate: audio.playbackRate });
      } catch (error) {
        /* 某些浏览器不支持 setPositionState */
      }
    }
  };

  const paintPlay = () => {
    playButton.replaceChildren(icon(audio.paused ? "play" : "pause", { size: 26 }));
    playButton.setAttribute("aria-label", audio.paused ? "播放" : "暂停");
  };

  // 播放状态行：源由常驻 session 负责，页面只负责显示
  const paintStatus = () => {
    if (session.key !== `${bookId}:${index}`) return;
    const stamp = timeline.audio.m4a_mtime;
    if (downloadIsCurrent(bookId, { index, m4a_mtime: stamp })) {
      status.replaceChildren(icon("circle-check", { size: 11 }), "已离线");
    } else if (session.buffering) {
      status.replaceChildren(icon("hourglass", { size: 11 }), "转码中…");
    } else {
      status.replaceChildren(icon("circle-check", { size: 11 }), "在线播放");
    }
  };

  // 听本章时后台把下一章转好（服务端已有新鲜 m4a 会直接秒回，不会重复转码），
  // 并把下一章的时间轴预取进内存、音频预取进浏览器缓存：续播时不再等网络。
  let warmedNext = false;
  const warmNext = () => {
    if (warmedNext || !next) return;
    warmedNext = true;
    if (!(next.m4a_ready && next.m4a_mtime)) {
      void api.prepareMobile(bookId, next.index, {})
        .then((info) => {
          // 和目录缓存是同一份对象：切到下一章时直接走"已转好"快路径
          next.m4a_ready = true;
          next.m4a_mtime = info.mtime;
          next.m4a_bytes = info.bytes;
          if (!downloadIsCurrent(bookId, next)) preloadAudio(bookId, next.index, info.mtime);
        })
        .catch(() => {});
    } else if (!downloadIsCurrent(bookId, next)) {
      preloadAudio(bookId, next.index, next.m4a_mtime);
    }
    void api.subtitles(bookId, next.index)
      .then((payload) => cacheTimeline(bookId, next.index, payload))
      .catch(() => {});
  };

  /* --- 事件 --- */

  // <audio> 现在是跨章节复用的，旧页面的监听必须在切页时解绑，否则会串章节。
  const abort = new AbortController();
  const listen = (target, type, handler, options) =>
    target.addEventListener(type, handler, { ...options, signal: abort.signal });

  playButton.addEventListener("click", () => (audio.paused ? void playAudio() : audio.pause()));
  listen(audio, "play", paintPlay);
  listen(audio, "play", warmNext);
  listen(audio, "pause", paintPlay);
  listen(audio, "timeupdate", () => {
    syncProgress();
    paintStatus();
  });
  listen(audio, "durationchange", () => {
    syncProgress();
    paintStatus();
  });
  listen(audio, "loadedmetadata", () => {
    audio.playbackRate = readRate();
    syncProgress();
    paintStatus();
  });

  seek.addEventListener("pointerdown", () => { dragging = true; });
  listen(window, "pointerup", () => { dragging = false; });
  // 手机上 PWA 是被系统直接杀掉的，不会走 teardown：切后台/关页面时就落一次记忆
  const remember = () => {
    // 已经切到别的章节时不要写：那会拿新 src 的 0 秒覆盖旧章节的进度
    if (session.key !== `${bookId}:${index}`) return;
    savePosition(bookId, index, audio.currentTime || 0, audio.playbackRate);
  };
  listen(window, "pagehide", remember);
  listen(document, "visibilitychange", () => {
    if (document.visibilityState === "hidden") remember();
  });
  seek.addEventListener("input", () => {
    elapsed.textContent = clock(seek.value);
  });
  seek.addEventListener("change", () => {
    audio.currentTime = Number(seek.value) || 0;
    follow = true;
  });

  stage.addEventListener("scroll", () => {
    if (Date.now() - autoScrollAt < 700) return;    // 自己滚的不算
    follow = false;
    followButton.hidden = false;
  });

  rateButton.addEventListener("click", () => {
    const nextRate = RATES[(RATES.indexOf(audio.playbackRate) + 1) % RATES.length];
    audio.defaultPlaybackRate = nextRate;
    audio.playbackRate = nextRate;
    writeRate(nextRate);
    rememberRate(bookId, nextRate);
    rateButton.textContent = `${nextRate}×`;
  });

  timerButton.addEventListener("click", () => {
    const options = [0, 15, 30, 60];
    sleep.minutes = options[(options.indexOf(sleep.minutes) + 1) % options.length];
    window.clearInterval(sleep.ticker);
    if (!sleep.minutes) {
      sleep.endsAt = 0;
      timerButton.textContent = "定时";
      toast("已取消定时停止");
      return;
    }
    sleep.endsAt = Date.now() + sleep.minutes * 60000;
    toast(`将在 ${sleep.minutes} 分钟后暂停`);
    sleep.ticker = window.setInterval(() => {
      const left = sleep.endsAt - Date.now();
      timerButton.textContent = left > 0 ? clock(left / 1000) : "定时";
      if (left <= 0) {
        window.clearInterval(sleep.ticker);
        audio.pause();
        sleep.minutes = 0;
        sleep.endsAt = 0;
        toast("定时结束，已暂停播放");
      }
    }, 1000);
  });

  listButton.addEventListener("click", () => {
    const host_ = h("div", { class: "listen__sheetHost" });
    const close = () => host_.remove();
    host_.addEventListener("click", (event) => { if (event.target === host_) close(); });
    const items = chapterList.map((chapter) =>
      h(
        "a",
        {
          class: `listen__sheetRow${chapter.index === index ? " is-current" : ""}`,
          href: `#/listen/${bookId}/${chapter.index}`,
          onClick: close,
        },
        h("span", { class: "mono muted" }, String(chapter.index).padStart(3, "0")),
        h("span", {}, chapter.title || `第 ${chapter.index} 章`),
        h("span", { class: "mono muted" }, clock(chapter.duration)),
      ),
    );
    host_.append(h("div", { class: "listen__sheetPanel" }, h("h3", { class: "letterpress" }, "章节"), ...items));
    document.body.append(host_);
    onTeardown(close);
  });

  const paintDownload = () => {
    const info = downloadInfo(bookId, index);
    downloadButton.disabled = false;
    if (downloadIsCurrent(bookId, { index, m4a_mtime: timeline.audio.m4a_mtime })) {
      downloadButton.replaceChildren(icon("circle-check", { size: 16 }));
      downloadButton.title = "已下载到本机；点击可重新下载或删除";
    } else if (info) {
      downloadButton.replaceChildren(icon("download", { size: 16 }));
      downloadButton.title = "服务器上的音频更新了，点一下重新下载";
    } else {
      downloadButton.replaceChildren(icon("download", { size: 16 }));
      downloadButton.title = `下载到手机（约 ${bytes(estimatedBytes)}）`;
    }
  };

  downloadButton.addEventListener("click", async () => {
    if (!offlineSupported()) {
      toast("离线下载需要 HTTPS 访问（局域网 http 只能在线听）", "error");
      return;
    }
    if (downloadIsCurrent(bookId, { index, m4a_mtime: timeline.audio.m4a_mtime })) {
      const again = await confirmDialog({
        title: "重新下载这一章？",
        message: "重新下载会覆盖手机里的离线包；要清空整本的离线包，去章节列表点「清空离线」。",
        confirmLabel: "重新下载",
      });
      if (!again) return;
    }
    downloadButton.disabled = true;
    try {
      await downloadChapter(bookId, { index, m4a_mtime: timeline.audio.m4a_mtime, estimated_bytes: estimatedBytes }, {
        force: true,
        onProgress: ({ phase, loaded, total }) => {
          if (phase === "prepare") downloadButton.replaceChildren(icon("hourglass", { size: 16 }));
          else if (phase === "download" && total) downloadButton.replaceChildren(document.createTextNode(`${Math.round((loaded / total) * 100)}%`));
        },
      });
      toast("已下载，可离线收听");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      paintDownload();
    }
  });

  paintPlay();
  paintDownload();
  paintStatus();
  raf = requestAnimationFrame(tick);
  syncProgress();
  // 已经在放（从悬浮球回到播放页 / 刚切章）就顺手把下一章转码 + 预取，续播更快
  warmNext();

  onTeardown(() => {
    cancelAnimationFrame(raf);
    window.clearInterval(sleep.ticker);
    remember();
    abort.abort();
    delete host.dataset.listen;
    // 关键：切页不暂停、不清 src —— 播放属于常驻 session，悬浮球随后接管控制。
    // 若是"播放页 → 播放页"，新页面会先一步把 pageToken 加一：样式留着，不闪顶栏/底栏。
    const token = pageToken;
    setTimeout(() => {
      if (pageToken !== token) return;
      document.body.classList.remove("is-listening");
      refreshMediaSession();
      paintMini();
    }, 0);
  });

  return root;
}
