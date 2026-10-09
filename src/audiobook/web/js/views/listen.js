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

export function render(host, route) {
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

function savePosition(bookId, index, seconds) {
  try {
    localStorage.setItem(`${POS_PREFIX}${bookId}:${index}`, String(Math.max(0, seconds)));
    localStorage.setItem(
      `${LAST_PREFIX}${bookId}`,
      JSON.stringify({ index, seconds: Math.max(0, seconds), at: Date.now() }),
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
      ? ` · 上次听到第 ${last.index} 章 ${clock(last.seconds)}`
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

  const last = readLast(bookId);
  const continueRow =
    last && chapters.some((chapter) => chapter.index === last.index)
      ? h(
          "a",
          { class: "listen__resume", href: `#/listen/${bookId}/${last.index}` },
          icon("play", { size: 14 }),
          `继续听 第 ${last.index} 章 · ${clock(last.seconds)}`,
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
        h("strong", {}, chapter.title || `第 ${chapter.index} 章`),
        h("span", { class: "mono muted" }, `${clock(chapter.duration)} · ${bytes(chapter.estimated_bytes)} · ${chapter.cues} 句`),
      ),
    ),
    button,
  );
  row.paint = paint;
  return row;
}

/* ------------------------------------------------------------------ 播放页 */

async function buildPlayer(host, bookId, index) {
  const [timeline, catalog] = await Promise.all([
    api.subtitles(bookId, index),
    api.listenCatalog(bookId).catch(() => null),
  ]);
  host.dataset.listen = "player";
  // 手机端播放页占满全屏：隐藏顶栏和底部导航（桌面端样式不动）
  document.body.classList.add("is-listening");

  const chapterList = catalog?.chapters || [];
  const position = chapterList.findIndex((chapter) => chapter.index === index);
  const prev = position > 0 ? chapterList[position - 1] : null;
  const next = position >= 0 && position < chapterList.length - 1 ? chapterList[position + 1] : null;
  const cues = timeline.cues || [];
  const estimatedBytes = Math.round(((timeline.duration || 0) * (timeline.audio.bitrate_kbps || 64) * 1000) / 8);

  const audio = new Audio();
  audio.preload = "metadata";
  audio.className = "listen__audio";
  audio.playbackRate = readRate();

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
          if (audio.paused) void start();
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
      onClick: () => prev && (window.location.hash = `#/listen/${bookId}/${prev.index}`),
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
      onClick: () => next && (window.location.hash = `#/listen/${bookId}/${next.index}`),
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

  const root = h("div", { class: "listen listen--player" }, bar, stage, followButton, controls, audio);

  /* --- 播放与高亮 --- */

  let follow = true;
  let activeIndex = -1;
  let autoScrollAt = 0;
  let lastSaved = 0;
  let raf = 0;
  let dragging = false;
  let disposed = false;
  let finished = false;

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
    const time = audio.currentTime || 0;
    paintActive(cueIndexAt(time));
    if (audio.paused) return;
    if (time - lastSaved > 5) {
      lastSaved = time;
      savePosition(bookId, index, time);
    }
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

  const start = async () => {
    try {
      await audio.play();
    } catch (error) {
      toast("浏览器拦住了播放，再点一次播放键", "error");
    }
  };

  const setSource = async () => {
    status.replaceChildren(icon("hourglass", { size: 11 }), "准备音频…");
    try {
      const info = await api.prepareMobile(bookId, index, {});
      audio.src = `${timeline.audio.m4a_url}?v=${info.mtime}`;
      status.replaceChildren(icon("circle-check", { size: 11 }), downloadIsCurrent(bookId, { index, m4a_mtime: info.mtime }) ? "已离线" : "在线播放");
    } catch (error) {
      // 离线 / 转码失败：退回已缓存或原始地址，让 service worker 兜底
      const stamp = timeline.audio.m4a_mtime ? `?v=${timeline.audio.m4a_mtime}` : "";
      audio.src = `${timeline.audio.m4a_url}${stamp}`;
      status.replaceChildren(icon("wifi-off", { size: 11 }), "离线播放");
    }
  };

  /* --- 事件 --- */

  playButton.addEventListener("click", () => (audio.paused ? start() : audio.pause()));
  audio.addEventListener("play", paintPlay);
  audio.addEventListener("pause", () => {
    paintPlay();
    if (!finished) savePosition(bookId, index, audio.currentTime || 0);
  });
  audio.addEventListener("timeupdate", syncProgress);
  audio.addEventListener("durationchange", syncProgress);
  audio.addEventListener("loadedmetadata", () => {
    syncProgress();
    const saved = readPosition(bookId, index);
    if (saved > 5 && saved < (audio.duration || timeline.duration) - 3) {
      audio.currentTime = saved;
      toast(`从上次位置 ${clock(saved)} 继续`);
    }
  });
  audio.addEventListener("ended", () => {
    finished = true;
    savePosition(bookId, index, 0);
    if (next) {
      toast(`第 ${index} 章播完，接着听第 ${next.index} 章`);
      window.location.hash = `#/listen/${bookId}/${next.index}`;
    }
  });
  audio.addEventListener("error", () => {
    if (disposed) return;
    if (audio.src) toast("音频加载失败：检查网络，或重新下载这一章", "error");
  });

  seek.addEventListener("pointerdown", () => { dragging = true; });
  window.addEventListener("pointerup", () => { dragging = false; });
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
    audio.playbackRate = nextRate;
    writeRate(nextRate);
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

  if ("mediaSession" in navigator) {
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: timeline.title || `第 ${index} 章`,
        artist: timeline.book_title || "",
        album: "AI 有声书",
        artwork: [{ src: "/static/icons/icon-512.png", sizes: "512x512", type: "image/png" }],
      });
      navigator.mediaSession.setActionHandler("play", () => start());
      navigator.mediaSession.setActionHandler("pause", () => audio.pause());
      navigator.mediaSession.setActionHandler("seekbackward", () => { audio.currentTime = Math.max(0, audio.currentTime - 15); });
      navigator.mediaSession.setActionHandler("seekforward", () => { audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 15); });
      navigator.mediaSession.setActionHandler("previoustrack", prev ? () => { window.location.hash = `#/listen/${bookId}/${prev.index}`; } : null);
      navigator.mediaSession.setActionHandler("nexttrack", next ? () => { window.location.hash = `#/listen/${bookId}/${next.index}`; } : null);
    } catch (error) {
      /* iOS 上部分 handler 不支持，忽略 */
    }
  }

  paintPlay();
  paintDownload();
  raf = requestAnimationFrame(tick);
  syncProgress();
  await setSource();

  onTeardown(() => {
    disposed = true;
    document.body.classList.remove("is-listening");
    cancelAnimationFrame(raf);
    window.clearInterval(sleep.ticker);
    if (!finished) savePosition(bookId, index, audio.currentTime || 0);
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    if ("mediaSession" in navigator) {
      navigator.mediaSession.metadata = null;
      try {
        navigator.mediaSession.setActionHandler("play", null);
        navigator.mediaSession.setActionHandler("pause", null);
        navigator.mediaSession.setActionHandler("nexttrack", null);
        navigator.mediaSession.setActionHandler("previoustrack", null);
      } catch (error) {
        /* 忽略 */
      }
    }
    delete host.dataset.listen;
  });

  return root;
}
