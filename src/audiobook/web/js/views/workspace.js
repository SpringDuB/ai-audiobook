// 书页工作台：左边章节目录、中间原文/角色文本、右边角色音色。
// 一屏之内完成"看章节 → 看分析 → 配音色"三件事。

import { api } from "../api.js";
import { duration } from "../format.js";
import { store } from "../store.js";
import { closeVoicePicker, loadVoices, openVoicePicker } from "../voicepicker.js";
import { emptyState, h, onTeardown, renderWithState, seal, toast } from "../ui.js";

const EMOTIONS = ["喜悦", "愤怒", "悲伤", "恐惧", "厌恶", "忧郁", "惊讶", "平静"];
const DELIVERIES = [
  ["normal", "正常"],
  ["shout", "喊叫"],
  ["whisper", "低语"],
  ["sneer", "冷笑"],
];

const TAB_LABELS = {
  lines: "角色文本",
  text: "原文",
};

function short(id) {
  return String(id || "").slice(0, 8);
}

function storedIndex(bookId) {
  const value = Number(window.localStorage?.getItem(`aiab:chapter:${bookId}`));
  return Number.isFinite(value) ? value : null;
}

function remember(bookId, index) {
  try {
    window.localStorage?.setItem(`aiab:chapter:${bookId}`, String(index));
  } catch {
    /* 隐身模式没有 localStorage，不影响使用 */
  }
}

function pickIndex(route, chapters) {
  const wanted = route.index ?? storedIndex(route.bookId);
  const found = chapters.find((chapter) => chapter.index === wanted);
  if (found) return found.index;
  const analyzed = chapters.find((chapter) => (chapter.lines || 0) > 0);
  return (analyzed || chapters[0]).index;
}

function paragraphs(content) {
  return String(content || "")
    .split(/\n\s*\n|\n/)
    .map((part) => part.trim())
    .filter(Boolean);
}

/* ---------------------------------------------------------------- 章节列表 */

function chapterItem(chapter, state, onSelect) {
  const active = chapter.index === state.index;
  return h(
    "button",
    {
      class: "chapter-item",
      type: "button",
      "aria-current": active ? "true" : null,
      dataset: { chapter: String(chapter.index) },
      onClick: () => onSelect(chapter.index),
    },
    h("span", { class: "chapter-item__no mono" }, chapter.index),
    h(
      "span",
      { class: "chapter-item__text" },
      h("span", { class: "chapter-item__title" }, chapter.title || `第${chapter.index}章`),
      h(
        "span",
        { class: "chapter-item__meta mono" },
        h("span", { class: "state-dot", dataset: { state: chapter.state } }),
        `${chapter.lines || 0} 句 · ${chapter.duration_sec ? duration(chapter.duration_sec) : "未出"} `,
      ),
    ),
  );
}

/* ---------------------------------------------------------------- 句子行 */

function metaChips(line) {
  const emotion = line.emotion || {};
  return [
    h(
      "span",
      { class: "line__who" },
      `${line.speaker_name || line.speaker}${line.addressee_name ? ` → ${line.addressee_name}` : ""}`,
    ),
    emotion.dominant ? h("span", { class: "tone-tag" }, `${emotion.dominant} ${emotion.intensity ?? ""}`.trim()) : null,
    line.delivery && line.delivery !== "normal" ? h("span", { class: "tag" }, line.delivery) : null,
    line.pause_after_ms ? h("span", { class: "tag mono" }, `停 ${line.pause_after_ms}ms`) : null,
    line.duration_sec ? h("span", { class: "tag mono" }, duration(line.duration_sec)) : null,
  ].filter(Boolean);
}

function lineEditor(line, bookId, onSaved, onCancel) {
  const text = h("textarea", { rows: 3, value: line.text });
  const speaker = h("input", { type: "text", value: line.speaker_name || line.speaker, placeholder: "说话人" });
  const addressee = h("input", { type: "text", value: line.addressee_name || line.addressee || "", placeholder: "受话人（可空）" });
  const emotion = h(
    "select",
    {},
    ...EMOTIONS.map((name) => h("option", { value: name, selected: (line.emotion || {}).dominant === name }, name)),
  );
  const intensity = h("input", { type: "number", min: "0", max: "1", step: "0.05", value: String((line.emotion || {}).intensity ?? 0.5) });
  const pause = h("input", { type: "number", min: "0", max: "5000", step: "10", value: String(line.pause_after_ms ?? 300) });
  const delivery = h(
    "select",
    {},
    ...DELIVERIES.map(([value, label]) => h("option", { value, selected: line.delivery === value }, label)),
  );
  const error = h("p", { class: "inline-error" });
  const save = h(
    "button",
    {
      class: "btn btn-primary btn-sm",
      type: "button",
      onClick: async (event) => {
        event.target.disabled = true;
        error.textContent = "";
        try {
          const result = await api.patchLine(bookId, line.id, {
            text: text.value,
            speaker: speaker.value.trim(),
            addressee: addressee.value.trim(),
            emotion: emotion.value,
            intensity: Number(intensity.value),
            delivery: delivery.value,
            pause_after_ms: Number(pause.value),
          });
          toast("已保存；本章成品已失效，重渲染后生效");
          onSaved(result.line);
        } catch (err) {
          error.textContent = err.message;
        } finally {
          event.target.disabled = false;
        }
      },
    },
    "保存",
  );
  return h(
    "div",
    { class: "line__edit" },
    h("div", { class: "field" }, h("label", {}, "台词"), text),
    h(
      "div",
      { class: "grid-3" },
      h("div", { class: "field" }, h("label", {}, "说话人"), speaker),
      h("div", { class: "field" }, h("label", {}, "受话人"), addressee),
      h("div", { class: "field" }, h("label", {}, "情绪"), emotion),
    ),
    h(
      "div",
      { class: "grid-3" },
      h("div", { class: "field" }, h("label", {}, "强度 0–1"), intensity),
      h("div", { class: "field" }, h("label", {}, "停顿 ms"), pause),
      h("div", { class: "field" }, h("label", {}, "语气"), delivery),
    ),
    h("div", { class: "row" }, save, h("button", { class: "btn btn-sm", type: "button", onClick: onCancel }, "取消"), error),
  );
}

function lineRow(line, ctx) {
  const body = h("div", { class: "line__body" });
  const playButton = h(
    "button",
    {
      class: "btn btn-sm",
      type: "button",
      disabled: !line.has_audio,
      title: line.has_audio ? "试听这句" : "这句还没合成",
      onClick: () => ctx.play(line, playButton),
    },
    line.has_audio ? "▶ 试听" : "未合成",
  );
  const renderRead = () => {
    body.replaceChildren(
      h("div", { class: "line__meta" }, ...metaChips(line)),
      h("p", { class: "line__text" }, line.text),
      h(
        "div",
        { class: "line__tools" },
        playButton,
        h("button", { class: "btn btn-sm", type: "button", onClick: () => renderEdit() }, "编辑"),
        h(
          "button",
          {
            class: "btn btn-sm",
            type: "button",
            onClick: async (event) => {
              event.target.disabled = true;
              try {
                await api.resynth(ctx.bookId, line.id);
                toast("已入队单句重合成");
              } catch (error) {
                toast(error.message, "error");
              } finally {
                event.target.disabled = false;
              }
            },
          },
          "重生成",
        ),
      ),
    );
  };
  const renderEdit = () => {
    body.replaceChildren(lineEditor(line, ctx.bookId, () => ctx.onChanged(), () => renderRead()));
  };
  renderRead();
  const row = h(
    "article",
    { class: "line", dataset: { lineId: line.id } },
    h("span", { class: "line__seq mono" }, String(ctx.displaySeq ?? line.seq ?? "").padStart(3, "0")),
    h("span", { class: "line__seal" }, seal(line.speaker_name || line.speaker)),
    body,
  );
  ctx.registry?.push({ row, line, renderEdit, renderRead });
  row.addEventListener("click", (event) => {
    if (event.target.closest("button")) return;
    const position = ctx.registry.findIndex((entry) => entry.row === row);
    if (position >= 0) ctx.focus?.(position);
  });
  return row;
}

/* ---------------------------------------------------------------- 选角面板 */

function roleRow(role, scope, ctx) {
  const voiceName = role.voice_name || role.voice_id || "未绑定";
  const bound = role.voice_id && role.voice_id !== "default";
  const chapters = role.chapters || [];
  return h(
    "article",
    { class: "cast-row", dataset: { roleId: role.role_id } },
    h("span", { class: "cast-row__seal" }, seal(role.name)),
    h(
      "div",
      { class: "cast-row__body" },
      h(
        "div",
        { class: "cast-row__head" },
        h("span", { class: "cast-row__name" }, role.name),
        h("span", { class: bound ? "tag" : "tag tag--alert", title: voiceName }, voiceName),
      ),
      h(
        "p",
        { class: "cast-row__meta mono" },
        scope === "chapter" ? `${role.lines} 句（本章）` : `${role.lines} 句 · ${chapters.length} 章`,
        role.score ? ` · 匹配 ${Math.round(role.score)}` : "",
      ),
    ),
    h(
      "button",
      {
        class: "btn btn-sm",
        type: "button",
        onClick: (event) => ctx.pick(role, event.currentTarget),
      },
      "换音色",
    ),
  );
}

/* ---------------------------------------------------------------- 主视图 */

async function build(route, host) {
  const bookId = route.bookId;
  const [chaptersPayload, bookPayload, castingPayload] = await Promise.all([
    api.chapters(bookId),
    api.book(bookId).catch(() => ({})),
    api.casting(bookId).catch(() => ({ roles: [] })),
  ]);
  const chapters = chaptersPayload.chapters || [];
  if (!chapters.length) {
    return emptyState("这本书还没有章节", "回书架导入 txt，或点「分析角色文本」重跑分章。");
  }
  const voiceLibrary = await loadVoices().catch(() => []);

  const state = {
    bookId,
    chapters,
    title: bookPayload.book?.title || short(bookId),
    casting: castingPayload.roles || [],
    index: pickIndex(route, chapters),
    tab: "lines",
    scope: "chapter",
    chapter: null,
  };
  let currentLines = [];
  let chapterRequest = 0;
  let registry = [];        // 当前章的行句柄，键盘 J/K/空格/Enter 用
  const cursor = { index: 0 };

  const chapterList = h("nav", { class: "chapter-list", "aria-label": "章节" });
  const scriptHead = h("header", { class: "script__head" });
  const scriptBody = h("section", { class: "script__body", "aria-live": "polite" });
  const castList = h("div", { class: "cast__list" });
  const castTabs = h("div", { class: "tabs tabs--sm" });
  const scriptTabs = h("div", { class: "tabs", role: "tablist" });
  const progress = h("span", { class: "workbench__progress mono", dataset: { book: bookId } }, "");
  const audio = new Audio();
  audio.preload = "none";
  onTeardown(() => {
    audio.pause();
    closeVoicePicker();
  });

  const play = (line, button) => {
    if (!line.has_audio) return;
    if (audio.dataset.lineId === line.id && !audio.paused) {
      audio.pause();
      button.textContent = "▶ 试听";
      return;
    }
    audio.dataset.lineId = line.id;
    audio.src = line.audio_url;
    audio.play().then(() => {
      button.textContent = "■ 停止";
    }).catch(() => toast("浏览器拦住了播放，再点一次", "error"));
  };
  audio.addEventListener("ended", () => {
    const row = scriptBody.querySelector(`[data-line-id="${audio.dataset.lineId}"] .line__tools .btn`);
    if (row) row.textContent = "▶ 试听";
  });

  /* --- 选角 --- */

  const chapterRoles = () => {
    const counts = new Map();
    for (const line of currentLines) {
      const roleId = line.speaker || "narrator";
      const bucket = counts.get(roleId) || { role_id: roleId, name: line.speaker_name || roleId, lines: 0 };
      bucket.lines += 1;
      counts.set(roleId, bucket);
    }
    const casted = new Map(state.casting.map((role) => [role.role_id, role]));
    return [...counts.values()]
      .map((role) => ({ score: 0, chapters: [], voice_id: "default", voice_name: "", ...casted.get(role.role_id), ...role }))
      .sort((a, b) => (a.role_id === "narrator" ? -1 : b.role_id === "narrator" ? 1 : b.lines - a.lines));
  };

  const pick = async (role, anchor) => {
    await openVoicePicker({
      anchor,
      roleName: role.name,
      currentVoiceId: role.voice_id,
      onPick: async (voice) => {
        const result = await api.setCasting(state.bookId, role.role_id, { voice_id: voice.id, voice_name: voice.name });
        const invalidated = result.invalidated || [];
        toast(
          invalidated.length
            ? `「${role.name}」→ ${voice.name}；第 ${invalidated.slice(0, 6).join("、")}${invalidated.length > 6 ? " 等" : ""} 章成品已失效，重渲染后生效`
            : `「${role.name}」→ ${voice.name}`,
        );
        await reloadCasting();
        await refreshChapters();
      },
    });
  };

  const paintCast = () => {
    castTabs.replaceChildren(
      h(
        "button",
        {
          class: "tab",
          type: "button",
          "aria-pressed": String(state.scope === "chapter"),
          onClick: () => {
            state.scope = "chapter";
            paintCast();
          },
        },
        `本章角色 ${chapterRoles().length}`,
      ),
      h(
        "button",
        {
          class: "tab",
          type: "button",
          "aria-pressed": String(state.scope === "book"),
          onClick: () => {
            state.scope = "book";
            paintCast();
          },
        },
        `全书角色 ${state.casting.length}`,
      ),
    );
    const roles = state.scope === "chapter" ? chapterRoles() : state.casting;
    castList.replaceChildren(
      ...(roles.length
        ? roles.map((role) => roleRow(role, state.scope, { pick }))
        : [h("p", { class: "muted" }, state.scope === "chapter" ? "这一章还没有标注结果。" : "全书还没有选角结果。")]),
    );
  };

  const reloadCasting = async () => {
    const payload = await api.casting(state.bookId).catch(() => null);
    if (payload) state.casting = payload.roles || [];
    paintCast();
  };

  /* --- 中间正文 --- */

  const paintScript = () => {
    scriptTabs.replaceChildren(
      ...Object.entries(TAB_LABELS).map(([key, label]) =>
        h(
          "button",
          {
            class: "tab",
            type: "button",
            "aria-pressed": String(state.tab === key),
            onClick: () => {
              state.tab = key;
              paintScript();
            },
          },
          label,
        ),
      ),
    );
    const chapter = state.chapter;
    if (!chapter) {
      scriptHead.replaceChildren();
      scriptBody.replaceChildren(h("p", { class: "muted" }, "读取中…"));
      return;
    }
    const ready = currentLines.filter((line) => line.has_audio).length;
    const total = currentLines.reduce((sum, line) => sum + (line.duration_sec || 0), 0);
    scriptHead.replaceChildren(
      h(
        "div",
        { class: "script__title" },
        h("h2", { class: "letterpress" }, chapter.title || `第${state.index}章`),
        h(
          "p",
          { class: "script__meta mono" },
          `${chapter.chars || 0} 字`,
          ` · ${currentLines.length} 句`,
          ` · 已合成 ${ready}/${currentLines.length}`,
          total ? ` · ${duration(total)}` : "",
        ),
      ),
      h("div", { class: "script__tools" }, scriptTabs),
    );
    if (state.tab === "text") {
      scriptBody.replaceChildren(
        ...(paragraphs(chapter.content).map((text) => h("p", { class: "raw-text" }, text)) || []),
      );
      if (!chapter.content) scriptBody.replaceChildren(h("p", { class: "muted" }, "这一章没有原文。"));
      return;
    }
    if (!currentLines.length) {
      scriptBody.replaceChildren(
        emptyState("这一章还没做逐句标注", "点上面的「分析角色文本」，worker 跑完就有了。"),
      );
      return;
    }
    const rows = [];
    registry = [];
    cursor.index = Math.min(cursor.index, Math.max(0, currentLines.length - 1));
    let lastScene = null;
    let position = 0;
    const sceneTitles = new Map((chapter.scenes || []).map((scene) => [scene.id, scene]));
    for (const line of currentLines) {
      if (line.scene && line.scene !== lastScene) {
        lastScene = line.scene;
        const scene = sceneTitles.get(line.scene);
        rows.push(
          h(
            "div",
            { class: "scene-divider" },
            h("span", { class: "mono" }, `场景 ${scene?.index ?? line.scene_index ?? "?"}`),
            h("span", {}, scene?.title || "（无标题）"),
            h("span", { class: "muted mono" }, `${(scene?.participants || []).map((person) => person.name).join(" / ") || "—"}`),
          ),
        );
      }
      position += 1;
      // 行号按"本章第几句"连续编号：analysis 里的 seq 是场景内序号，直接显示会一段一段重来
      rows.push(
        lineRow(line, {
          bookId: state.bookId,
          displaySeq: position,
          play,
          registry,
          focus: (target) => {
            cursor.index = target;
            highlight();
          },
          onChanged: () => reloadChapter(state.index),
        }),
      );
    }
    scriptBody.replaceChildren(...rows);
    highlight();
  };

  const highlight = () => {
    registry.forEach((entry, position) => entry.row.classList.toggle("is-current", position === cursor.index));
  };

  const move = (delta) => {
    if (!registry.length) return;
    cursor.index = Math.max(0, Math.min(registry.length - 1, cursor.index + delta));
    highlight();
    registry[cursor.index].row.scrollIntoView({ block: "nearest" });
  };

  scriptBody.tabIndex = 0;
  scriptBody.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select, audio, button")) return;
    const item = registry[cursor.index];
    if (event.key === "j" || event.key === "J") {
      event.preventDefault();
      move(1);
    } else if (event.key === "k" || event.key === "K") {
      event.preventDefault();
      move(-1);
    } else if (event.key === " " && item?.line.has_audio) {
      event.preventDefault();
      play(item.line, item.row.querySelector(".line__tools .btn"));
    } else if (event.key === "Enter" && item) {
      event.preventDefault();
      item.renderEdit();
    } else if (event.key === "Escape" && item) {
      item.renderRead();
    }
  });

  /* --- 数据加载 --- */

  const reloadChapter = async (index) => {
    const mine = ++chapterRequest;
    state.index = index;
    remember(bookId, index);
    scriptBody.replaceChildren(h("p", { class: "muted" }, "读取中…"));
    paintChapterList();
    const [textPayload, scenesPayload, linesPayload] = await Promise.all([
      api.chapterText(bookId, index),
      api.scenes(bookId, index).catch(() => ({ scenes: [] })),
      api.lines(bookId, index),
    ]);
    if (mine !== chapterRequest) return;
    state.chapter = {
      title: textPayload.title,
      content: textPayload.content,
      chars: textPayload.chars,
      scenes: scenesPayload.scenes || [],
    };
    currentLines = linesPayload.lines || [];
    paintScript();
    paintCast();
  };

  const refreshChapters = async () => {
    const payload = await api.chapters(bookId).catch(() => null);
    if (!payload) return;
    state.chapters = payload.chapters || [];
    paintChapterList();
  };

  const paintChapterList = () => {
    chapterList.replaceChildren(...state.chapters.map((chapter) => chapterItem(chapter, state, selectChapter)));
  };

  const selectChapter = (index) => {
    if (index === state.index && state.chapter) return;
    closeVoicePicker();
    window.history.replaceState(null, "", `#/book/${bookId}/chapter/${index}`);
    reloadChapter(index);
  };

  /* --- 顶部工具条 --- */

  const action = (label, fn, { primary = false } = {}) =>
    h(
      "button",
      {
        class: primary ? "btn btn-primary" : "btn",
        type: "button",
        onClick: async (event) => {
          const button = event.currentTarget;
          button.disabled = true;
          try {
            await fn();
          } catch (error) {
            toast(error.message, "error");
          } finally {
            button.disabled = false;
          }
        },
      },
      label,
    );

  const queued = (result, what) =>
    toast(result.queued ? `已入队${what} ${result.queued} 个任务，进度看右上角` : `${what}没有需要补的任务`);

  const bar = h(
    "header",
    { class: "workbench__bar" },
    h(
      "div",
      { class: "workbench__title" },
      h("a", { class: "muted", href: "#/shelf" }, "书架"),
      h("span", { class: "muted" }, " / "),
      h("span", { class: "letterpress" }, state.title),
    ),
    h(
      "div",
      { class: "workbench__actions" },
      progress,
      action("分析角色文本", async () => queued(await api.analyzeBook(bookId), "分析")),
      action("生成有声书", async () => queued(await api.generateBook(bookId), "合成"), { primary: true }),
      action(
        "导出成品",
        async () => {
          await api.exportBook(bookId, { mode: "all" });
          toast("已入队整本导出");
        },
      ),
      action("重渲染本章", async () => {
        await api.renderChapter(bookId, state.index);
        toast("已入队本章重渲染");
        await refreshChapters();
      }),
      h("a", { class: "btn btn-ghost", href: `#/book/${bookId}/issues` }, "异常"),
    ),
  );

  /* --- 组装 --- */

  const container = h(
    "div",
    { class: "workbench" },
    bar,
    h(
      "div",
      { class: "workbench__body" },
      h(
        "aside",
        { class: "workbench__chapters" },
        h(
          "div",
          { class: "workbench__chapters-head" },
          h("span", { class: "mono muted" }, `章节 ${state.chapters.length}`),
          h(
            "button",
            { class: "btn btn-sm btn-ghost", type: "button", onClick: () => refreshChapters() },
            "刷新",
          ),
        ),
        chapterList,
      ),
      h("section", { class: "workbench__script" }, scriptHead, scriptBody),
      h(
        "aside",
        { class: "workbench__cast" },
        h(
          "div",
          { class: "cast__head" },
          h("h3", { class: "letterpress" }, "角色音色"),
          h("span", { class: "mono muted" }, `音色库 ${voiceLibrary.length}`),
        ),
        castTabs,
        castList,
        h(
          "p",
          { class: "cast__foot muted" },
          "点「换音色」从悬浮窗挑；改完的章节要重渲染才生效。",
          h("a", { class: "row", href: "#/voices" }, "去音色库试听 →"),
        ),
      ),
    ),
  );

  paintChapterList();
  paintCast();
  await reloadChapter(state.index);

  // 右上角进度：跟着 SSE 里这本书的任务走；跑任务时顺手刷新章节状态
  let stopWatch = null;
  let lastRefresh = 0;
  const stopStore = store.subscribe((current) => {
    if (!document.body.contains(progress)) {
      if (stopWatch) stopWatch();
      return;
    }
    const mine = current.jobs.filter((job) => job.book_id === bookId);
    const running = mine.find((job) => job.status === "running");
    const queued = mine.filter((job) => job.status === "queued").length;
    if (running) {
      const jobProgress = running.progress || {};
      progress.textContent = `${running.kind} ${jobProgress.done ?? 0}/${jobProgress.total ?? 0}${queued ? ` · 排队 ${queued}` : ""}`;
    } else {
      progress.textContent = queued ? `排队 ${queued}` : "";
    }
    if ((running || queued) && Date.now() - lastRefresh > 5000) {
      lastRefresh = Date.now();
      refreshChapters();
    }
  });
  stopWatch = stopStore;
  onTeardown(stopStore);
  return container;
}

export function render(host, route) {
  return renderWithState(host, () => build(route, host));
}
