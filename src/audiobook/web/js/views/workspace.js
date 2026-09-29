// 书页工作台：左边章节目录、中间原文/角色文本、右边角色音色。
// 一屏之内完成"看章节 → 看分析 → 配音色"三件事。

import { api } from "../api.js";
import { duration } from "../format.js";
import { store } from "../store.js";
import { closeVoicePicker, loadVoices, openVoicePicker } from "../voicepicker.js";
import { chapterPickerDialog, emptyState, h, onTeardown, renderWithState, seal, toast } from "../ui.js";

const EMOTIONS = ["喜悦", "愤怒", "悲伤", "恐惧", "厌恶", "忧郁", "惊讶", "平静"];
const DELIVERIES = [
  ["normal", "正常"],
  ["shout", "喊叫"],
  ["whisper", "低语"],
  ["sneer", "冷笑"],
];

// 页签顺序就是显示顺序：默认停在「原文」，想看逐句标注再切「角色文本」
const TAB_LABELS = {
  text: "原文",
  lines: "角色文本",
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

/* 角色配色：同一个角色在整章里颜色稳定，旁白保持中性 */
const ROLE_HUES = 8;

function roleHue(roleId) {
  const key = String(roleId || "");
  if (!key || key === "narrator") return "narrator";
  let hash = 0;
  for (const char of key) hash = (hash * 31 + char.codePointAt(0)) % 1000003;
  return String(hash % ROLE_HUES);
}

function metaChips(line) {
  const emotion = line.emotion || {};
  return [
    h(
      "span",
      { class: "line__who" },
      line.speaker_name || line.speaker,
    ),
    emotion.dominant ? h("span", { class: "tone-tag" }, `${emotion.dominant} ${emotion.intensity ?? ""}`.trim()) : null,
    line.delivery && line.delivery !== "normal" ? h("span", { class: "tag" }, line.delivery) : null,
    line.duration_sec ? h("span", { class: "tag mono" }, duration(line.duration_sec)) : null,
  ].filter(Boolean);
}

function lineEditor(line, bookId, onSaved, onCancel) {
  const text = h("textarea", { rows: 3, value: line.text });
  const speaker = h("input", { type: "text", value: line.speaker_name || line.speaker, placeholder: "说话人" });
  const emotion = h(
    "select",
    {},
    ...EMOTIONS.map((name) => h("option", { value: name, selected: (line.emotion || {}).dominant === name }, name)),
  );
  const intensity = h("input", { type: "number", min: "0", max: "1", step: "0.05", value: String((line.emotion || {}).intensity ?? 0.5) });
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
            emotion: emotion.value,
            intensity: Number(intensity.value),
            delivery: delivery.value,
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
      h("div", { class: "field" }, h("label", {}, "情绪"), emotion),
      h("div", { class: "field" }, h("label", {}, "强度 0–1"), intensity),
    ),
    h(
      "div",
      { class: "grid-3" },
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
    { class: "line", dataset: { lineId: line.id, hue: roleHue(line.speaker) } },
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
  // 绑了但音色库里找不到（被停用/删掉）——不早点提示，合成时会一片红
  const boundVoice = bound ? ctx.voiceById.get(role.voice_id) : null;
  const missing = Boolean(bound && !boundVoice);
  const chapters = role.chapters || [];
  const recommendations = (role.recommendations || []).filter((item) => item.voice_id);
  const picked = recommendations.findIndex((item) => item.voice_id === role.voice_id);
  const badge = role.source === "manual" ? "已手选" : picked === 0 ? "按推荐" : picked > 0 ? "推荐备选" : "";
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
        h(
          "span",
          {
            class: bound && !missing ? "tag" : "tag tag--alert",
            title: missing ? `${voiceName} 已停用或不在音色库里：换一个音色再生成` : voiceName,
          },
          missing ? `${voiceName}（已停用）` : voiceName,
        ),
        badge ? h("span", { class: "tag tag--rec" }, badge) : null,
      ),
      h(
        "p",
        { class: "cast-row__meta mono" },
        scope === "chapter" ? `${role.lines} 句（本章）` : `${role.lines} 句 · ${chapters.length} 章`,
        picked >= 0 && recommendations[picked].reason ? ` · ${recommendations[picked].reason}` : "",
      ),
      recommendations.length
        ? h(
            "div",
            { class: "cast-row__recs" },
            ...recommendations.map((item, position) => {
              const voice = ctx.voiceById.get(item.voice_id);
              const playable = Boolean(voice?.has_ref);
              return h(
                "span",
                { class: "rec-chip" },
                h(
                  "button",
                  {
                    class: "chip",
                    type: "button",
                    title: item.reason || "",
                    "aria-pressed": String(item.voice_id === role.voice_id),
                    onClick: () => ctx.pickRecommended(role, item),
                  },
                  `${position + 1}. ${item.voice_name || item.voice_id}`,
                  item.confidence
                    ? h("span", { class: "chip__num mono" }, ` ${Math.round(Number(item.confidence) * 100)}%`)
                    : null,
                ),
                h(
                  "button",
                  {
                    class: "rec-play",
                    type: "button",
                    disabled: !playable,
                    title: playable ? `试听「${voice.name}」的参考音频` : "这个音色没有参考音频，试听不了",
                    "aria-label": `试听 ${item.voice_name || item.voice_id}`,
                    onClick: (event) => ctx.preview(item.voice_id, event.currentTarget),
                  },
                  "▶",
                ),
              );
            }),
          )
        : null,
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

// 刚导入：分章还在 worker 队列里跑。这里先显示等待页并自己轮询，
// 而不是让书页因为"章节还没落盘"直接报错。
function pendingView(route, host, title, payload) {
  const hint = h("p", { class: "muted" }, payload.pending ? "正在分章…（导入后自动进行，几秒到几十秒）" : "这本书还没有章节。");
  const node = h(
    "div",
    { class: "workbench workbench--pending" },
    h(
      "header",
      { class: "workbench__bar" },
      h(
        "div",
        { class: "workbench__id" },
        h(
          "div",
          { class: "workbench__title" },
          h("a", { class: "workbench__slash", href: "#/shelf" }, "书架 /"),
          h("span", { class: "workbench__book", title }, title),
        ),
      ),
    ),
    h("div", { class: "empty" }, h("p", {}, "正在分章…"), hint),
  );
  let tries = 0;
  const timer = window.setInterval(async () => {
    tries += 1;
    const latest = await api.chapters(route.bookId).catch(() => null);
    if (latest?.chapters?.length) {
      window.clearInterval(timer);
      render(host, route);
      return;
    }
    if (tries >= 120) {   // 约 3 分钟：别再无声地转下去
      window.clearInterval(timer);
      hint.textContent = "分章还没完成，去「任务中心」看看是不是有失败的任务。";
    }
  }, 1500);
  onTeardown(() => window.clearInterval(timer));
  return node;
}

async function build(route, host) {
  const bookId = route.bookId;
  const [chaptersPayload, bookPayload, castingPayload] = await Promise.all([
    api.chapters(bookId),
    api.book(bookId).catch(() => ({})),
    api.casting(bookId).catch(() => ({ roles: [] })),
  ]);
  const chapters = chaptersPayload.chapters || [];
  if (!chapters.length) {
    return pendingView(route, host, bookPayload.book?.title || short(bookId), chaptersPayload);
  }
  const voiceLibrary = await loadVoices().catch(() => []);
  const voiceById = new Map(voiceLibrary.map((voice) => [voice.id, voice]));

  // 推荐音色的试听：和正文试听分开一个 audio，互不打断
  const previewAudio = new Audio();
  previewAudio.preload = "none";
  const resetPreviewButtons = () => {
    document.querySelectorAll(".rec-play[data-playing='1']").forEach((node) => {
      node.dataset.playing = "0";
      node.textContent = "▶";
    });
  };
  const previewVoice = (voiceId, button) => {
    const voice = voiceById.get(voiceId);
    if (!voice?.has_ref) {
      toast("这个音色缺少参考音频，试听不了", "error");
      return;
    }
    if (button.dataset.playing === "1") {
      previewAudio.pause();
      resetPreviewButtons();
      return;
    }
    resetPreviewButtons();
    previewAudio.src = voice.sample_url;
    previewAudio.play().catch(() => toast("浏览器拦住了播放，再点一次", "error"));
    button.dataset.playing = "1";
    button.textContent = "■";
  };
  previewAudio.addEventListener("ended", resetPreviewButtons);
  onTeardown(() => {
    previewAudio.pause();
    previewAudio.removeEventListener("ended", resetPreviewButtons);
  });

  const state = {
    bookId,
    chapters,
    title: bookPayload.book?.title || short(bookId),
    casting: castingPayload.roles || [],
    index: pickIndex(route, chapters),
    tab: "text",
    scope: "chapter",
    chapter: null,
    output: bookPayload.output || null,
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
      recommended: role.recommendations || [],
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

  // 点推荐音色：直接用这一条（不打开悬浮窗），用户不选时用的就是第一条
  const pickRecommended = (role, item) => {
    Promise.resolve(
      api
        .setCasting(state.bookId, role.role_id, { voice_id: item.voice_id, voice_name: item.voice_name || item.voice_id })
        .then(async (result) => {
          const invalidated = result.invalidated || [];
          toast(
            invalidated.length
              ? `「${role.name}」→ ${item.voice_name || item.voice_id}；第 ${invalidated.slice(0, 6).join("、")}${invalidated.length > 6 ? " 等" : ""} 章成品已失效，重渲染后生效`
              : `「${role.name}」→ ${item.voice_name || item.voice_id}`,
          );
          await reloadCasting();
          await refreshChapters();
        }),
    ).catch((error) => toast(error.message, "error"));
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
        ? roles.map((role) => roleRow(role, state.scope, { pick, pickRecommended, preview: previewVoice, voiceById }))
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
    let position = 0;
    for (const line of currentLines) {
      position += 1;
      // 行号按"本章第几句"连续编号
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
    const [textPayload, linesPayload] = await Promise.all([
      api.chapterText(bookId, index),
      api.lines(bookId, index),
    ]);
    if (mine !== chapterRequest) return;
    state.chapter = {
      title: textPayload.title,
      content: textPayload.content,
      chars: textPayload.chars,
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

  const action = (label, fn, { primary = false, title = "", menu = false } = {}) =>
    h(
      "button",
      {
        class: menu ? "menu__item" : primary ? "btn btn-primary" : "btn",
        type: "button",
        title,
        onClick: async (event) => {
          const button = event.currentTarget;
          button.disabled = true;
          try {
            await fn();
          } catch (error) {
            toast(error.message, "error");
          } finally {
            button.disabled = false;
            closeMore();
          }
        },
      },
      label,
    );

  const queued = (result, what) =>
    toast(result.queued ? `已入队${what} ${result.queued} 个任务，进度看右上角` : `${what}没有需要补的任务`);

  // 导出与返工收进「更多」：外部常驻的四步是 单章分析 / 单章音频 / 整本分析 / 整本音频
  const closeMore = () => {
    if (more) more.open = false;
  };
  const openFolder = action(
    "打开成果文件夹",
    async () => {
      const result = await api.revealOutput(bookId);
      toast(result.opened ? `已在文件管理器里打开 ${result.dir}` : `没打开成功，产物在 ${result.dir}`);
    },
    {
      menu: true,
      title: "在系统文件管理器里打开 output/：整本 wav / srt / mkv、分章文件与播放列表都在里面",
    },
  );
  const paintOutputAction = () => {
    const ready = Boolean(state.output?.exists);
    openFolder.disabled = !ready;
    openFolder.title = ready
      ? "在系统文件管理器里打开 output/：整本 wav / srt / mkv、分章文件与播放列表都在里面"
      : "还没有导出产物：先点上面的「导出整本成品」";
  };
  paintOutputAction();
  const more = h(
    "details",
    { class: "workbench__more" },
    h("summary", { title: "导出成品、打开成果文件夹、重新拼接本章、异常清单" }, "更多 ▾"),
    h(
      "div",
      { class: "menu", role: "menu" },
      action(
        "导出整本成品",
        async () => {
          await api.exportBook(bookId, { mode: "all" });
          toast("已入队整本导出；跑完可以在「更多 ▾」里打开成果文件夹");
        },
        { menu: true, title: "把已生成的章节合成整本产物：book.wav、字幕、mkv、播放列表等，写进 output/" },
      ),
      openFolder,
      action(
        "重新拼接本章",
        async () => {
          await api.renderChapter(bookId, state.index);
          toast("已入队本章重渲染");
          await refreshChapters();
        },
        { menu: true, title: "不重新跑 TTS：只把本章已有的逐句音频重新拼接、对齐字幕、做响度归一" },
      ),
      h(
        "a",
        { class: "menu__item", href: `#/book/${bookId}/issues`, title: "降级与失败记录，可按类型批量重试" },
        "异常清单",
      ),
    ),
  );

  const bar = h(
    "header",
    { class: "workbench__bar" },
    h(
      "div",
      { class: "workbench__id" },
      h(
        "div",
        { class: "workbench__title" },
        h("a", { class: "workbench__slash", href: "#/shelf" }, "书架 /"),
        h("span", { class: "workbench__book", title: state.title }, state.title),
        h("span", { class: "workbench__chap" }, `· 第 ${state.index} 章`),
      ),
      h(
        "div",
        { class: "workbench__steps", "aria-hidden": "true" },
        h("span", { class: "workbench__step", dataset: { state: "current" } }, "① 分析台词"),
        h("span", { class: "workbench__step" }, "② 生成音频"),
        h("span", { class: "workbench__step" }, "③ 导出成品"),
        progress,
      ),
    ),
    h(
      "div",
      { class: "workbench__actions" },
      action("分析本章台词", async () => queued(await api.analyzeChapter(bookId, state.index), "本章分析"), {
        title: "只重跑当前这一章的逐句标注（谁说的 + 什么情绪），其他章不动；已人工改过的这一章会被覆盖",
      }),
      action("生成本章音频", async () => queued(await api.generateChapter(bookId, state.index), "本章合成"), {
        title: "只合成当前这一章：逐句 TTS → 拼接出本章音频与字幕，其他章不动",
      }),
      action(
        "分析全本台词",
        async () => {
          // 弹窗多选章节：默认勾上还没分析的章，也可以只挑几章返工
          const pending = state.chapters.filter((chapter) => !Number(chapter.lines || 0)).map((chapter) => chapter.index);
          const picked = await chapterPickerDialog({
            title: "分析哪些章节的台词？",
            message:
              "整本重跑勾选章节的提取（说话人 + 情绪）。已勾选且已有标注的章节会被覆盖（含人工修改），" +
              "对应成品音频随之失效，需要重新生成。",
            chapters: state.chapters,
            confirmLabel: "开始分析",
            selected: pending.length ? pending : state.chapters.map((chapter) => chapter.index),
          });
          if (!picked || !picked.length) return;
          queued(await api.analyzeChapters(bookId, picked), "章节分析");
          await refreshChapters();
        },
        {
          title: "整本重跑逐句标注，并重推角色音色（新称呼会并进角色表）；弹窗里可以只勾几章返工",
        },
      ),
      action("生成整本音频", async () => queued(await api.generateBook(bookId), "合成"), {
        primary: true,
        title: "全书逐句合成 + 拼接成整本成品；还没分析过的章节会自动先补分析",
      }),
      more,
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
          "点推荐音色直接选中，点 ▶ 先试听这段参考音频；换完的章节要重新生成才生效。",
          h("a", { class: "row", href: "#/voices" }, "去音色库上传 / 停用音色 →"),
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
  let wasBusy = false;
  const seenExports = new Set();
  let exportBaseline = null;
  const refreshOutput = async () => {
    try {
      state.output = await api.output(bookId);
      paintOutputAction();
    } catch {
      /* 还没有 output/ 目录不算错 */
    }
  };
  const stopStore = store.subscribe((current) => {
    if (!document.body.contains(progress)) {
      if (stopWatch) stopWatch();
      return;
    }
    const mine = current.jobs.filter((job) => job.book_id === bookId);
    // 整本导出跑完：亮起「打开成果文件夹」，并提示一声
    const exportsDone = mine.filter((job) => job.kind === "book_export" && job.status === "done");
    if (exportBaseline === null) {
      exportsDone.forEach((job) => seenExports.add(job.id));   // 首次订阅时把历史任务当基线，不弹提示
      exportBaseline = true;
    } else {
      for (const job of exportsDone) {
        if (seenExports.has(job.id)) continue;
        seenExports.add(job.id);
        refreshOutput();
        toast("整本导出完成，可在「更多 ▾」里打开成果文件夹");
      }
    }
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
    // 任务跑完的那一刻刷新一次：本章重分析/重渲染的结果要立刻出现在页面上
    const busy = Boolean(running) || queued > 0;
    if (wasBusy && !busy) {
      refreshChapters();
      reloadChapter(state.index);
    }
    wasBusy = busy;
  });
  stopWatch = stopStore;
  onTeardown(stopStore);

  // 顶栏实际高度写回 --bar-h：排版区与右侧栏的吸顶位置都跟着它走，
  // 不再写死 116px，书名换行、字号变化、窄屏两行布局都不会错位。
  const barHeight = () => {
    const height = Math.round(bar.getBoundingClientRect().height);
    if (height) document.documentElement.style.setProperty("--bar-h", `${height}px`);
  };
  barHeight();
  if (typeof ResizeObserver === "function") {
    const observer = new ResizeObserver(barHeight);
    observer.observe(bar);
    onTeardown(() => observer.disconnect());
  }

  return container;
}

export function render(host, route) {
  return renderWithState(host, () => build(route, host));
}
