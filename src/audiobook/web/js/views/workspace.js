// 书页工作台：左边章节目录、中间原文/角色文本、右边角色音色。
// 一屏之内完成"看章节 → 看分析 → 配音色"三件事。

import { api } from "../api.js";
import { duration, kindLabel } from "../format.js";
import { icon } from "../icons.js";
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

// 分析类任务 → 章节状态：正在分析台词 / 等待分析。SSE 每秒推一次任务快照。
const ANALYSIS_KINDS = new Set(["characters", "chapters", "lines"]);

function analysisActivity(jobs, chapters) {
  const activity = new Map();
  const mark = (index, value) => {
    if (!Number.isFinite(index)) return;
    if (activity.get(index) === "running") return;   // 正在跑的最高优先，不被排队态覆盖
    activity.set(index, value);
  };
  for (const job of jobs) {
    if (!ANALYSIS_KINDS.has(job.kind)) continue;
    if (job.status !== "running" && job.status !== "queued") continue;
    const queued = job.status === "queued";
    if (job.kind === "lines") {
      mark(Number(job.chapter_index), queued ? "queued" : "running");
      continue;
    }
    if (job.kind === "chapters") {
      // 一个批量 job 内部并发提多章：progress.chapters_inflight 是此刻真正在跑的章
      const inflight = new Set((job.progress?.chapters_inflight || []).map(Number));
      const extracted = new Set((job.progress?.chapters_done || []).map(Number));
      for (const raw of job.payload?.chapters || []) {
        const index = Number(raw);
        // 这一章已经提取完：不再显示状态（章节 meta 里的「已分析」才是结果）
        if (extracted.has(index)) continue;
        mark(index, !queued && inflight.has(index) ? "running" : "queued");
      }
      continue;
    }
    // characters（整本）：progress.chapters_inflight 就是此刻正在提取的章
    const inflight = new Set(job.progress?.chapters_inflight || []);
    const extracted = new Set(job.progress?.chapters_done || []);
    for (const chapter of chapters) {
      if (extracted.has(Number(chapter.index))) continue;
      mark(chapter.index, inflight.has(chapter.index) && !queued ? "running" : "queued");
    }
  }
  return activity;
}

function chapterItem(chapter, state, onSelect, activity) {
  const active = chapter.index === state.index;
  const analyzing = activity?.get(chapter.index);
  const analyzed = Number(chapter.lines || 0) > 0;
  const audioReady = Number(chapter.segments || 0) > 0 || chapter.state === "rendered";
  const audioLabel = !audioReady
    ? "未生成"
    : chapter.lines && Number(chapter.segments || 0) < Number(chapter.lines)
      ? `已生成 ${chapter.segments}/${chapter.lines}`
      : "已生成";
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
        `${analyzed ? "已分析" : "未分析"} · ${audioLabel}`,
        chapter.duration_sec ? ` · ${duration(chapter.duration_sec)}` : "",
      ),
      // 只有"正在提取"（沙漏）和"确实还没分析、在排队等着"才显示；
      // 已经分析好的章不再显示状态——meta 里的「已分析」就是结论
      analyzing === "running" || (analyzing === "queued" && !analyzed)
        ? h(
            "span",
            {
              class: `chapter-item__status${analyzing === "running" ? " chapter-item__status--busy" : ""}`,
              title: analyzing === "running" ? "大模型正在提取这一章的说话人与情绪" : "已排进分析队列，等前面的章跑完",
            },
            icon(analyzing === "running" ? "hourglass" : "ellipsis", { size: 12 }),
            analyzing === "running" ? "正在分析台词" : "等待分析",
          )
        : null,
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
  // 正在合成的段落挂沙漏（转圈），排队中的挂灰字；有音频了按钮就地变成「试听」
  const statusChip = () => {
    if (!line.pending) return null;
    const busy = line.pending === "inflight";
    return h(
      "span",
      {
        class: `line__status${busy ? " line__status--busy" : ""}`,
        title: busy ? "正在生成这句的音频" : "已排进队列，等 TTS 空闲",
      },
      icon(busy ? "hourglass" : "ellipsis", { size: 12 }),
      busy ? "生成中" : "排队中",
    );
  };
  const renderRead = () => {
    entry.editing = false;
    const playButton = h(
      "button",
      {
        class: "btn btn-sm",
        type: "button",
        disabled: !line.has_audio,
        title: line.has_audio ? "试听这句" : "这句还没合成",
        onClick: () => ctx.play(line, playButton),
      },
      icon("play", { size: 12 }),
      line.has_audio ? "试听" : "未合成",
    );
    body.replaceChildren(
      h("div", { class: "line__meta" }, ...metaChips(line)),
      h("p", { class: "line__text" }, line.text),
      h(
        "div",
        { class: "line__tools" },
        statusChip(),
        playButton,
        h("button", { class: "btn btn-sm", type: "button", onClick: () => renderEdit() }, icon("pencil", { size: 12 }), "编辑"),
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
          icon("rotate-ccw", { size: 12 }),
          "重生成",
        ),
      ),
    );
  };
  const renderEdit = () => {
    entry.editing = true;
    body.replaceChildren(lineEditor(line, ctx.bookId, () => ctx.onChanged(), () => renderRead()));
  };
  const entry = { row: null, line, editing: false };
  renderRead();
  const row = h(
    "article",
    { class: "line", dataset: { lineId: line.id, hue: roleHue(line.speaker) } },
    h("span", { class: "line__seq mono" }, String(ctx.displaySeq ?? line.seq ?? "").padStart(3, "0")),
    h("span", { class: "line__seal" }, seal(line.speaker_name || line.speaker)),
    body,
  );
  entry.row = row;
  entry.renderEdit = renderEdit;
  entry.renderRead = renderRead;
  ctx.registry?.push(entry);
  row.addEventListener("click", (event) => {
    if (event.target.closest("button")) return;
    const position = ctx.registry.findIndex((entry) => entry.row === row);
    if (position >= 0) ctx.focus?.(position);
  });
  return row;
}

/* ---------------------------------------------------------------- 选角面板 */

function roleRow(role, scope, ctx) {
  const chapters = role.chapters || [];
  const described = Boolean((role.description || "").trim());
  const isLibrary = role.voice_source === "library";
  // 试听状态：没生成过 / 生成过但描述改了（过期）
  const stale = described && role.has_preview && role.preview_current === false;
  const never = described && !role.has_preview;
  const editor = h("textarea", {
    class: "cast-row__desc",
    rows: "3",
    value: role.description || "",
    placeholder: "这个角色的音色：年龄、音色质地、说话习惯、气质……（逐句生成时和每句话的表演描述拼在一起）",
    "aria-label": `${role.name} 的音色描述`,
    onKeydown: (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        ctx.saveDescription(role, editor.value, event.currentTarget);
      }
    },
  });
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
        isLibrary
          ? h(
              "span",
              { class: "tag", title: "这个角色手工绑定了库存音色：合成走克隆，不用描述" },
              `库存音色 ${role.voice_name || role.voice_id}`,
            )
          : h(
              "span",
              {
                class: described ? "tag tag--rec" : "tag tag--alert",
                title: described ? "音色描述已就绪" : "还没有音色描述：点「让模型写一版」或自己写",
              },
              described ? "设计音色" : "待写描述",
            ),
        stale
          ? h("span", { class: "tag tag--alert", title: "描述改过了：重新生成试听才生效" }, "试听过期")
          : never
            ? h("span", { class: "tag", title: "还没生成过试听：点「试听」按当前描述生成一段" }, "未试听")
            : null,
      ),
      h(
        "p",
        { class: "cast-row__meta mono" },
        scope === "chapter" ? `${role.lines} 句（本章）` : `${role.lines} 句 · ${chapters.length} 章`,
      ),
      isLibrary
        ? null
        : h(
            "div",
            { class: "cast-row__design" },
            editor,
            role.sample ? h("p", { class: "cast-row__sample" }, `试音台词：${role.sample}`) : null,
            h(
              "div",
              { class: "cast-row__actions" },
              h(
                "button",
                {
                  class: "btn btn-sm",
                  type: "button",
                  title: "按当前描述生成一段试听（角色第一次试听要等几秒）",
                  onClick: (event) => ctx.previewRole(role, editor.value, event.currentTarget),
                },
                icon("play", { size: 12 }),
                "试听",
              ),
              h(
                "button",
                {
                  class: "btn btn-sm",
                  type: "button",
                  title: "保存描述（Ctrl+Enter）：保存后这个角色的旧音频会按新描述重生成",
                  onClick: (event) => ctx.saveDescription(role, editor.value, event.currentTarget),
                },
                icon("check", { size: 12 }),
                "保存描述",
              ),
              h(
                "button",
                {
                  class: "btn btn-sm",
                  type: "button",
                  title: "让大模型根据这个角色的台词重写一版描述",
                  onClick: (event) => ctx.rewriteDescription(role, editor, event.currentTarget),
                },
                icon("refresh-cw", { size: 12 }),
                "让模型写一版",
              ),
              h(
                "button",
                {
                  class: "btn btn-sm cast-row__bind",
                  type: "button",
                  title: "改用音色库里的一段参考音频克隆这个角色（备用通道）",
                  onClick: (event) => ctx.pick(role, event.currentTarget),
                },
                icon("library", { size: 12 }),
                "绑库存音色",
              ),
            ),
          ),
    ),
    null,
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
      node.replaceChildren(icon("play", { size: 12 }));
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
    button.replaceChildren(icon("square", { size: 12 }));
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
  // 逐句试听按钮的两种状态：▶ 试听 / ■ 停止
  const setPlayLabel = (button, playing) => {
    button.replaceChildren(icon(playing ? "square" : "play", { size: 12 }), playing ? "停止" : "试听");
  };

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
      setPlayLabel(button, false);
      return;
    }
    audio.dataset.lineId = line.id;
    // mtime 当版本号：单句重生成后路径不变，带上它才不会放上一版的缓存
    audio.src = line.audio_mtime ? `${line.audio_url}?v=${line.audio_mtime}` : line.audio_url;
    audio.play().then(() => {
      setPlayLabel(button, true);
    }).catch(() => toast("浏览器拦住了播放，再点一次", "error"));
  };
  audio.addEventListener("ended", () => {
    const row = scriptBody.querySelector(`[data-line-id="${audio.dataset.lineId}"] .line__tools .btn`);
    if (row) setPlayLabel(row, false);
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

  // 保存角色的基础音色描述：描述变了 → 这个角色的旧音频会按新描述重生成
  const saveDescription = async (role, value, button) => {
    const description = (value || "").trim();
    if (!description) {
      toast("音色描述不能为空", "error");
      return;
    }
    if (description === (role.description || "").trim()) {
      toast("描述没变");
      return;
    }
    if (button) button.disabled = true;
    try {
      await api.saveRoleDescription(state.bookId, role.role_id, { description });
      toast(`「${role.name}」的音色描述已保存；重新生成音频时会用新描述`);
      await reloadCasting();
      await refreshChapters();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      if (button) button.disabled = false;
    }
  };

  // 让大模型重写一版描述（异步任务）
  const rewriteDescription = async (role, editor, button) => {
    if (button) button.disabled = true;
    try {
      await api.rewriteRoleDescription(state.bookId, role.role_id);
      toast(`已让模型重写「${role.name}」的音色描述，跑完自动刷新`);
      const timer = window.setInterval(async () => {
        const payload = await api.casting(state.bookId).catch(() => null);
        if (!payload) return;
        const latest = (payload.roles || []).find((item) => item.role_id === role.role_id);
        if (latest && (latest.description || "") !== (role.description || "")) {
          window.clearInterval(timer);
          state.casting = payload.roles || [];
          paintCast();
          toast(`「${role.name}」的新描述已写好`);
        }
      }, 2000);
      onTeardown(() => window.clearInterval(timer));
    } catch (error) {
      toast(error.message, "error");
    } finally {
      if (button) button.disabled = false;
    }
  };

  // 角色试听：按当前描述生成一段（没生成过才真的跑模型，之后直接听缓存文件）
  const previewRole = async (role, value, button) => {
    const description = (value || "").trim() || role.description || "";
    if (!description) {
      toast("先写一段音色描述再试听", "error");
      return;
    }
    // 描述没动过、试听又是按这份描述生成的就直接放缓存文件，别再跑一遍模型
    const fresh = role.has_preview && role.preview_current && description === (role.description || "").trim();
    if (fresh) {
      resetPreviewButtons();
      previewAudio.src = api.rolePreviewUrl(state.bookId, role.role_id, role.updated_at || Date.now());
      await previewAudio.play().catch(() => toast("浏览器拦住了播放，再点一次", "error"));
      return;
    }
    if (button) {
      button.disabled = true;
      button.dataset.label = button.textContent;
      button.replaceChildren(icon("hourglass", { size: 12 }), "生成中…");
    }
    try {
      const result = await api.previewRole(state.bookId, role.role_id, { description });
      resetPreviewButtons();
      previewAudio.src = api.rolePreviewUrl(state.bookId, role.role_id, result.updated_at);
      await previewAudio.play().catch(() => toast("浏览器拦住了播放，再点一次", "error"));
      await reloadCasting();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      if (button) {
        button.disabled = false;
        button.replaceChildren(icon("play", { size: 12 }), "试听");
      }
    }
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
        ? roles.map((role) =>
            roleRow(role, state.scope, {
              pick,
              pickRecommended,
              preview: previewVoice,
              voiceById,
              saveDescription,
              rewriteDescription,
              previewRole,
            }),
          )
        : [h("p", { class: "muted" }, state.scope === "chapter" ? "这一章还没有标注结果。" : "全书还没有选角结果。")]),
    );
  };

  const reloadCasting = async () => {
    const payload = await api.casting(state.bookId).catch(() => null);
    if (payload) state.casting = payload.roles || [];
    paintCast();
  };

  /* --- 中间正文 --- */

  // 标题栏（章节名 + 已合成 x/y）单独抽出来：合成过程中每出一句就更新一次，
  // 不用整块重建正文，滚动位置和光标都不会跳。
  const paintScriptHead = () => {
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
    if (!chapter) return scriptHead.replaceChildren();
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
  };

  const paintScript = () => {
    paintScriptHead();
    const chapter = state.chapter;
    if (!chapter) {
      scriptBody.replaceChildren(h("p", { class: "muted" }, "读取中…"));
      return;
    }
    if (state.tab === "text") {
      registry = [];        // 原文页签没有行句柄，别把旧句柄留在列表里
      scriptBody.replaceChildren(
        ...(paragraphs(chapter.content).map((text) => h("p", { class: "raw-text" }, text)) || []),
      );
      if (!chapter.content) scriptBody.replaceChildren(h("p", { class: "muted" }, "这一章没有原文。"));
      return;
    }
    if (!currentLines.length) {
      const activity = chapterActivity.get(state.index);
      scriptBody.replaceChildren(
        activity
          ? emptyState(
              activity === "running" ? "正在分析这一章的台词…" : "这一章已排进分析队列",
              "大模型在逐句判定说话人与情绪，跑完这里会自动出现角色文本，不用刷新页面。",
            )
          : emptyState("这一章还没做逐句标注", "点上面的「分析本章台词」，worker 跑完就有了。"),
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

  /* --- 逐句实时进度：正在生成的那句转沙漏，出音频就地亮起来 --- */

  const SYNTH_KINDS = new Set(["synthesize", "synthesize_line"]);

  const pendingForChapter = (jobs) => {
    const pending = new Map();
    for (const job of jobs) {
      if (job.chapter_index !== state.index) continue;
      if (job.kind === "synthesize_line") {
        const lineId = (job.progress && job.progress.pending_line) || null;
        if (lineId) pending.set(lineId, "inflight");
        continue;
      }
      const inflight = new Set((job.progress && job.progress.inflight) || []);
      for (const line of currentLines) {
        if (line.has_audio) continue;
        pending.set(line.id, inflight.has(line.id) ? "inflight" : "queued");
      }
    }
    return pending;
  };

  const paintPending = (pending) => {
    for (const entry of registry) {
      const next = pending.get(entry.line.id) || null;
      if (entry.line.pending === next) continue;
      entry.line.pending = next;
      if (!entry.editing) entry.renderRead();
    }
  };

  // 把最新一份 lines 合并进当前行：只有"有没有音频/音频版本"变了才重画那一行，
  // 编辑中的行不碰（别把用户正在改的内容冲掉）。
  const mergeLines = (fresh) => {
    if (fresh.length !== registry.length) {
      currentLines = fresh;
      paintScript();
      return;
    }
    const byId = new Map(fresh.map((line) => [line.id, line]));
    let audioChanged = false;
    for (const entry of registry) {
      const next = byId.get(entry.line.id);
      if (!next) continue;
      const before = `${entry.line.has_audio}|${entry.line.audio_mtime}`;
      Object.assign(entry.line, next);
      if (`${entry.line.has_audio}|${entry.line.audio_mtime}` !== before) {
        audioChanged = true;
        if (!entry.editing) entry.renderRead();
      }
    }
    currentLines = fresh;
    if (audioChanged) paintScriptHead();
  };

  const pullLines = async (index) => {
    const payload = await api.lines(bookId, index).catch(() => null);
    if (!payload || index !== state.index) return;
    mergeLines(payload.lines || []);
  };

  let linePollAt = 0;
  let linePolling = false;
  const syncSynthProgress = (jobs, { force = false } = {}) => {
    const active = jobs.filter((job) => SYNTH_KINDS.has(job.kind) && job.status === "running");
    paintPending(pendingForChapter(active));
    if (!active.length) return;
    if (!force && (linePolling || Date.now() - linePollAt < 1000)) return;
    linePollAt = Date.now();
    linePolling = true;
    pullLines(state.index).finally(() => {
      linePolling = false;
    });
  };

  let chapterActivity = new Map();
  const paintChapterList = (activity = chapterActivity) => {
    chapterActivity = activity;
    chapterList.replaceChildren(
      ...state.chapters.map((chapter) => chapterItem(chapter, state, selectChapter, chapterActivity)),
    );
  };

  const selectChapter = (index) => {
    if (index === state.index && state.chapter) return;
    closeVoicePicker();
    window.history.replaceState(null, "", `#/book/${bookId}/chapter/${index}`);
    reloadChapter(index);
  };

  /* --- 顶部工具条 --- */

  const action = (label, fn, { primary = false, title = "", menu = false, glyph = "" } = {}) =>
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
      glyph ? icon(glyph, { size: 14 }) : null,
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
      glyph: "folder-open",
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
    h("summary", { title: "导出成品、打开成果文件夹、重新拼接本章、异常清单" }, "更多", icon("ellipsis", { size: 14 })),
    h(
      "div",
      { class: "menu", role: "menu" },
      action(
        "导出整本成品",
        async () => {
          await api.exportBook(bookId, { mode: "all" });
          toast("已入队整本导出；跑完可以在「更多 ▾」里打开成果文件夹");
        },
        {
          menu: true,
          glyph: "package",
          title: "把已生成的章节合成整本产物：book.wav、字幕、mkv、播放列表等，写进 output/",
        },
      ),
      openFolder,
      action(
        "重新拼接本章",
        async () => {
          await api.renderChapter(bookId, state.index);
          toast("已入队本章重渲染");
          await refreshChapters();
        },
        {
          menu: true,
          glyph: "refresh-cw",
          title: "不重新跑 TTS：只把本章已有的逐句音频重新拼接、对齐字幕、做响度归一",
        },
      ),
      h(
        "a",
        { class: "menu__item", href: `#/book/${bookId}/issues`, title: "降级与失败记录，可按类型批量重试" },
        icon("triangle-alert", { size: 14 }),
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
        glyph: "scan-text",
        title: "只重跑当前这一章的逐句标注（谁说的 + 什么情绪），其他章不动；已人工改过的这一章会被覆盖",
      }),
      action("生成本章音频", async () => queued(await api.generateChapter(bookId, state.index), "本章合成"), {
        glyph: "file-audio",
        title:
          "只合成当前这一章：逐句 TTS → 拼接出本章音频与字幕，其他章不动；" +
          "已经有音频的句子直接复用，只有换过音色的句子才重合成",
      }),
      action(
        "分析全本台词",
        async () => {
          // 弹窗多选章节：默认勾上还没分析的章，也可以只挑几章返工
          const pending = state.chapters.filter((chapter) => !Number(chapter.lines || 0)).map((chapter) => chapter.index);
          const choice = await chapterPickerDialog({
            title: "分析哪些章节的台词？",
            message:
              "默认只补还没分析过的章节：已经分析好的会直接跳过，不重复花大模型调用。" +
              "要重算已完成的章节（含人工修改），勾上最下面的「覆盖重跑」——" +
              "它会轮到哪一章才覆盖哪一章，没轮到的旧结果还在，中途取消不丢。",
            chapters: state.chapters,
            confirmLabel: "开始分析",
            rerunLabel: "覆盖重跑：勾选章节里已分析好的部分也重新分析（覆盖旧标注）",
            selected: pending.length ? pending : state.chapters.map((chapter) => chapter.index),
          });
          if (!choice || !choice.picked.length) return;
          queued(await api.analyzeChapters(bookId, choice.picked, choice.force), "章节分析");
          await refreshChapters();
        },
        {
          glyph: "book-open-text",
          title: "补跑逐句标注（说话人 + 情绪）；已分析好的章节自动跳过，弹窗里可以只勾几章，或勾「覆盖重跑」返工",
        },
      ),
      action("生成整本音频", async () => queued(await api.generateBook(bookId), "合成"), {
        primary: true,
        glyph: "headphones",
        title:
          "只生成已经分析好的章节：逐句合成 → 拼接成整本成品；没分析的章自动跳过，" +
          "已经有音频的句子不重跑，只有换过音色的句子才重合成",
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
            icon("refresh-cw", { size: 12 }),
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
          "每个角色一段音色描述：点「试听」按当前描述生成一段，改完描述点「保存描述」；"
            + "描述一改，这个角色的旧音频会在下次生成时按新描述重跑。",
          h("a", { class: "row", href: "#/voices" }, icon("audio-lines", { size: 12 }), "去音色库上传 / 停用音色"),
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
  let analysisPollAt = 0;
  let wasBusy = false;
  let activitySignature = "";
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
    // 章节列表上的"正在分析台词 / 等待分析"：只在状态变化时重画，避免每秒重建 DOM
    const activity = analysisActivity(mine, state.chapters);
    const signature = [...activity.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([index, value]) => `${index}:${value}`)
      .join(",");
    if (signature !== activitySignature) {
      const before = chapterActivity.get(state.index) || "";
      activitySignature = signature;
      paintChapterList(activity);
      // 当前章正在被分析（且还没有角色文本）→ 正文区显示进度提示
      const now = activity.get(state.index) || "";
      if (before !== now && !currentLines.length) paintScript();
    }
    if (activity.get(state.index) === "running" && Date.now() - analysisPollAt > 1500) {
      analysisPollAt = Date.now();
      pullLines(state.index);
    }
    // 正在合成的章节：每秒拉一次逐句状态，句子一完成立刻能试听，不用刷新页面
    syncSynthProgress(mine);
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
      progress.textContent = `${kindLabel(running.kind)} ${jobProgress.done ?? 0}/${jobProgress.total ?? 0}${queued ? ` · 排队 ${queued}` : ""}`;
    } else {
      progress.textContent = queued ? `排队 ${queued}` : "";
    }
    if ((running || queued) && Date.now() - lastRefresh > 5000) {
      lastRefresh = Date.now();
      const viewing = state.index;
      const before = Number(state.chapters.find((c) => c.index === viewing)?.lines || 0);
      refreshChapters().then(() => {
        // 整本分析是边提取边落章的：当前这章刚出结果就顺手把它刷出来，不用等整本跑完
        const after = Number(state.chapters.find((c) => c.index === viewing)?.lines || 0);
        if (state.index === viewing && after > before) reloadChapter(viewing);
      });
    }
    // 任务跑完的那一刻刷新一次：本章重分析/重渲染的结果要立刻出现在页面上
    const busy = Boolean(running) || queued > 0;
    if (wasBusy && !busy) {
      refreshChapters();
      reloadChapter(state.index);
      // 选角任务跑完：角色栏立刻换成新的推荐音色（不刷新页面）
      reloadCasting();
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
