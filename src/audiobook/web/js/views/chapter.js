import { api } from "../api.js";
import { duration } from "../format.js";
import { emptyState, h, renderWithState, seal, toast } from "../ui.js";

const EMOTIONS = ["喜悦", "愤怒", "悲伤", "恐惧", "厌恶", "忧郁", "惊讶", "平静"];
const DELIVERIES = [
  ["normal", "正常"],
  ["shout", "喊叫"],
  ["whisper", "低语"],
  ["sneer", "冷笑"],
];

function metaChips(line) {
  const emotion = line.emotion || {};
  return [
    h("span", { class: "proof-line__who" }, `${line.speaker_name || line.speaker}${line.addressee_name ? ` → ${line.addressee_name}` : ""}`),
    emotion.dominant ? h("span", { class: "tone-tag" }, `${emotion.dominant} ${emotion.intensity ?? ""}`.trim()) : null,
    line.delivery && line.delivery !== "normal" ? h("span", { class: "tag" }, line.delivery) : null,
    h("span", { class: "tag mono" }, `停顿 ${line.pause_after_ms ?? 0}ms`),
    line.duration_sec ? h("span", { class: "tag mono" }, duration(line.duration_sec)) : null,
  ].filter(Boolean);
}

function editorFor(line, bookId, onSaved, onCancel) {
  const text = h("textarea", { rows: 3, value: line.text });
  const speaker = h(
    "input",
    { type: "text", value: line.speaker_name || line.speaker, placeholder: "说话人（角色名或 id）" },
  );
  const addressee = h("input", {
    type: "text",
    value: line.addressee_name || line.addressee || "",
    placeholder: "受话人（可空）",
  });
  const emotion = h(
    "select",
    {},
    ...EMOTIONS.map((name) =>
      h("option", { value: name, selected: (line.emotion || {}).dominant === name }, name),
    ),
  );
  const intensity = h("input", {
    type: "number",
    min: "0",
    max: "1",
    step: "0.05",
    value: String((line.emotion || {}).intensity ?? 0.5),
  });
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
    { class: "proof-line__edit" },
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
    h("div", { class: "row" }, save, h("button", { class: "btn btn-sm", onClick: onCancel }, "取消"), error),
  );
}

function proofLine(line, bookId, state, refresh) {
  const body = h("div", { class: "proof-line__body" });
  const renderRead = () => {
    const audio = h("audio", {
      controls: true,
      preload: "none",
      src: line.has_audio ? line.audio_url : null,
    });
    if (!line.has_audio) audio.disabled = true;
    state.audio = audio;
    body.replaceChildren(
      h("div", { class: "proof-line__meta" }, ...metaChips(line)),
      h("p", { class: "proof-line__text" }, line.text),
      h(
        "div",
        { class: "proof-line__tools" },
        audio,
        h("button", { class: "btn btn-sm", onClick: () => renderEdit() }, "编辑"),
        h(
          "button",
          {
            class: "btn btn-sm",
            onClick: async (event) => {
              event.target.disabled = true;
              try {
                await api.resynth(bookId, line.id);
                toast("已入队单句重合成，去任务中心看进度");
              } catch (error) {
                toast(error.message, "error");
              } finally {
                event.target.disabled = false;
              }
            },
          },
          "重生成这句",
        ),
        line.has_audio ? null : h("span", { class: "muted" }, "未合成"),
      ),
    );
  };
  const renderEdit = () => {
    body.replaceChildren(
      editorFor(
        line,
        bookId,
        () => refresh(),
        () => renderRead(),
      ),
    );
  };
  renderRead();
  const row = h(
    "article",
    { class: "proof-line", dataset: { lineId: line.id }, tabIndex: -1 },
    h("span", { class: "proof-line__seq mono" }, String(line.seq).padStart(3, "0")),
    h("span", { class: "proof-line__seal" }, seal(line.speaker_name || line.speaker)),
    body,
  );
  row.addEventListener("click", () => {
    state.current = row;
  });
  state.rows.push({ row, line, renderRead, renderEdit });
  return row;
}

function sceneCard(scene) {
  return h(
    "article",
    { class: "sheet" },
    h(
      "div",
      { class: "scene-card__head" },
      h("h3", { class: "letterpress" }, `${scene.index}. ${scene.title || "（无标题）"}`),
      h(
        "div",
        { class: "scene-card__people" },
        ...(scene.participants || []).map((person) => h("span", { class: "row" }, seal(person.name), h("span", { class: "muted" }, person.name))),
      ),
    ),
    h(
      "p",
      { class: "scene-card__summary" },
      scene.summary || "（无摘要）",
    ),
    h(
      "div",
      { class: "stat-line mono" },
      `句 ${scene.lines}`,
      `时长 ${duration(scene.duration_sec)}`,
      scene.tone?.dominant ? `基调 ${scene.tone.dominant} ${scene.tone.intensity ?? ""}` : null,
      scene.audio_ready ? "音频就绪" : "待合成",
    ),
  );
}

async function build(route, host) {
  const { bookId, index } = route;
  const [scenesPayload, linesPayload, chaptersPayload] = await Promise.all([
    api.scenes(bookId, index),
    api.lines(bookId, index),
    api.chapters(bookId).catch(() => ({ chapters: [] })),
  ]);
  const chapterMeta = (chaptersPayload.chapters || []).find((item) => item.index === index);
  const lines = linesPayload.lines || [];
  const scenes = scenesPayload.scenes || [];
  const refresh = () => render(host, route);
  const state = { rows: [], current: null, audio: null, filter: null };

  const list = h("div", { class: "proof" });
  const draw = () => {
    state.rows = [];
    list.replaceChildren(
      ...(state.filter ? lines.filter((line) => line.scene === state.filter) : lines).map((line) =>
        proofLine(line, bookId, state, refresh),
      ),
    );
  };
  const chips = h(
    "div",
    { class: "scene-rail" },
    h(
      "button",
      {
        class: "scene-chip",
        "aria-pressed": String(state.filter === null),
        onClick: () => {
          state.filter = null;
          chips.querySelectorAll(".scene-chip").forEach((node) => node.setAttribute("aria-pressed", "false"));
          chips.firstChild.setAttribute("aria-pressed", "true");
          draw();
        },
      },
      h("span", { class: "scene-chip__title" }, "全部句子"),
      h("span", { class: "scene-chip__meta mono" }, `${lines.length} 句`),
    ),
    ...scenes.map((scene) =>
      h(
        "button",
        {
          class: "scene-chip",
          "aria-pressed": "false",
          onClick: (event) => {
            state.filter = scene.id;
            chips.querySelectorAll(".scene-chip").forEach((node) => node.setAttribute("aria-pressed", "false"));
            event.currentTarget.setAttribute("aria-pressed", "true");
            draw();
          },
        },
        h("span", { class: "scene-chip__title" }, scene.title || `场景 ${scene.index}`),
        h("span", { class: "scene-chip__meta mono" }, `${scene.lines} 句 · ${duration(scene.duration_sec)}`),
      ),
    ),
  );

  const container = h(
    "div",
    {},
    h(
      "div",
      { class: "page-head" },
      h(
        "div",
        { class: "page-head__title" },
        h(
          "p",
          { class: "muted" },
          h("a", { href: "#/shelf" }, "书架"),
          " / ",
          h("a", { href: `#/book/${bookId}` }, short(bookId)),
          ` / 第 ${index} 章`,
        ),
        h("h1", { class: "letterpress" }, scenesPayload.title || `第 ${index} 章`),
      ),
      h(
        "div",
        { class: "page-head__actions" },
        chapterMeta?.duration_sec
          ? h("audio", { controls: true, preload: "none", src: `/api/books/${bookId}/chapters/${index}/audio` })
          : null,
        h(
          "button",
          {
            class: "btn",
            onClick: async () => {
              try {
                await api.renderChapter(bookId, index);
                toast("已入队本章重渲染");
                window.location.hash = "#/jobs";
              } catch (error) {
                toast(error.message, "error");
              }
            },
          },
          "重渲染本章",
        ),
        h(
          "button",
          {
            class: "btn btn-ghost",
            onClick: async () => {
              try {
                await api.runBook(bookId);
                toast("已按断点补排队列");
                window.location.hash = "#/jobs";
              } catch (error) {
                toast(error.message, "error");
              }
            },
          },
          "继续生成",
        ),
      ),
    ),
    h(
      "p",
      { class: "muted" },
      "快捷键：",
      h("span", { class: "kbd" }, "J"),
      " / ",
      h("span", { class: "kbd" }, "K"),
      " 切换句子，",
      h("span", { class: "kbd" }, "空格"),
      " 试听，",
      h("span", { class: "kbd" }, "Enter"),
      " 编辑，",
      h("span", { class: "kbd" }, "Esc"),
      " 取消。",
    ),
  );

  if (!lines.length) {
    container.append(emptyState("这一章还没有句子", "先跑分析（书架页的一键分析），或等 worker 处理完。"));
    return container;
  }

  const layout = h(
    "div",
    { class: "chapter-layout" },
    chips,
    h(
      "div",
      { class: "stack" },
      scenes.length
        ? h(
            "details",
            { class: "sheet scene-overview" },
            h(
              "summary",
              {},
              `场景概览 · ${scenes.length} 个场景 · ${duration(scenes.reduce((sum, scene) => sum + (scene.duration_sec || 0), 0))}`,
            ),
            h("div", { class: "scene-cards" }, ...scenes.map(sceneCard)),
          )
        : null,
      list,
    ),
  );
  container.append(layout);
  draw();

  const select = (delta) => {
    if (!state.rows.length) return;
    const position = state.rows.findIndex((item) => item.row === state.current);
    const next = Math.max(0, Math.min(state.rows.length - 1, (position === -1 ? -1 + (delta > 0 ? 1 : 0) : position + delta)));
    state.current = state.rows[next].row;
    state.current.focus();
  };
  container.tabIndex = 0;
  container.addEventListener("keydown", (event) => {
    if (event.target.closest("input, textarea, select, audio")) return;
    const current = state.rows.find((item) => item.row === state.current);
    if (event.key === "j" || event.key === "J") select(1);
    else if (event.key === "k" || event.key === "K") select(-1);
    else if (event.key === " " && current?.line.has_audio) {
      event.preventDefault();
      const audio = current.row.querySelector("audio");
      if (audio) {
        audio.currentTime = 0;
        audio.play().catch(() => toast("浏览器拦住了自动播放，点一下播放按钮", "error"));
      }
    } else if (event.key === "Enter" && current) current.renderEdit();
    else if (event.key === "Escape" && current) current.renderRead();
  });
  return container;
}

function short(id) {
  return String(id || "").slice(0, 8);
}

export function render(host, route) {
  return renderWithState(host, () => build(route, host));
}
