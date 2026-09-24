import { api } from "../api.js";
import { h, renderWithState, toast } from "../ui.js";

const GROUPS = [
  {
    title: "模型与 LLM",
    fields: [
      { key: "engine", label: "合成引擎", type: "select", options: [["fake", "fake（无 GPU 假引擎）"], ["http", "http（独立 TTS 服务）"]] },
      { key: "llm_base_url", label: "LLM 端点", type: "text" },
      { key: "llm_model", label: "LLM 模型", type: "text" },
      { key: "llm_temperature", label: "温度", type: "number", step: "0.1", min: "0", max: "2" },
      { key: "llm_concurrency", label: "LLM 并发", type: "number", min: "1" },
    ],
  },
  {
    title: "TTS 服务",
    fields: [
      { key: "tts_endpoints", label: "端点（一行一个）", type: "list" },
      { key: "synth_concurrency", label: "合成并发（服务端未自报时的默认值）", type: "number", min: "1" },
      { key: "synth_concurrency_max", label: "并发安全上限", type: "number", min: "1" },
      { key: "ffmpeg_path", label: "ffmpeg 路径（留空=PATH）", type: "text" },
    ],
  },
  {
    title: "输出与响度",
    fields: [
      { key: "loudness_mode", label: "响度模式", type: "select", options: [["lufs", "lufs（-16 LUFS 默认）"], ["rms", "rms（有声书平台 -18~-23 dB）"], ["off", "off（草稿，不归一）"]] },
      { key: "loudness_target_lufs", label: "目标 LUFS", type: "number", step: "0.5" },
      { key: "loudness_true_peak", label: "真峰值上限 dBTP", type: "number", step: "0.1" },
      { key: "loudness_rms_target_db", label: "RMS 目标 dB", type: "number", step: "0.5" },
      { key: "export_container", label: "章节容器", type: "select", options: [["mkv", "mkv（快，软字幕）"], ["mp4", "mp4（相册/剪映友好）"]] },
      { key: "export_mkv", label: "生成章节容器", type: "checkbox" },
      { key: "export_target_sample_rate", label: "目标采样率（0=自动）", type: "number", min: "0" },
    ],
  },
  {
    title: "停顿",
    fields: [
      { key: "pause_scale", label: "整体缩放", type: "number", step: "0.05", min: "0" },
      { key: "pause_min_ms", label: "停顿下限 ms", type: "number", min: "0" },
      { key: "pause_max_ms", label: "停顿上限 ms", type: "number", min: "0" },
      { key: "pause_scene_extra_ms", label: "场景切换额外停顿 ms", type: "number", min: "0" },
      { key: "pause_tail_ms", label: "章节末尾额外静音 ms", type: "number", min: "0" },
    ],
  },
];

function fieldNode(field, value) {
  const holder = h("div", { class: "field" });
  if (field.type === "checkbox") {
    const input = h("input", { type: "checkbox", checked: Boolean(value), dataset: { key: field.key } });
    holder.append(h("label", { class: "row" }, input, field.label));
    return holder;
  }
  holder.append(h("label", { for: `f-${field.key}` }, field.label));
  if (field.type === "select") {
    holder.append(
      h(
        "select",
        { id: `f-${field.key}`, dataset: { key: field.key } },
        ...field.options.map(([optionValue, label]) => h("option", { value: optionValue, selected: String(value) === optionValue }, label)),
      ),
    );
    return holder;
  }
  if (field.type === "list") {
    holder.append(
      h("textarea", { id: `f-${field.key}`, rows: 3, dataset: { key: field.key }, value: (value || []).join("\n") }),
    );
    return holder;
  }
  holder.append(
    h("input", {
      id: `f-${field.key}`,
      type: field.type,
      step: field.step || null,
      min: field.min || null,
      max: field.max || null,
      value: value === null || value === undefined ? "" : String(value),
      dataset: { key: field.key },
    }),
  );
  return holder;
}

function collect(container) {
  const patch = {};
  for (const node of container.querySelectorAll("[data-key]")) {
    const key = node.dataset.key;
    if (node.type === "checkbox") patch[key] = node.checked;
    else if (node.tagName === "TEXTAREA") patch[key] = node.value.split("\n").map((line) => line.trim()).filter(Boolean);
    else if (node.type === "number") patch[key] = node.value === "" ? 0 : Number(node.value);
    else patch[key] = node.value;
  }
  return patch;
}

async function build() {
  const payload = await api.settings();
  const settings = payload.settings || {};
  const saved = new Set(payload.overlay_keys || []);
  const body = h("div");
  let dirty = false;
  const status = h("p", { class: "muted" }, saved.size ? `已在界面上改过：${[...saved].join("、")}` : "目前全部来自 .env / 环境变量");

  for (const group of GROUPS) {
    const grid = h("div", { class: "grid-2" }, ...group.fields.map((field) => fieldNode(field, settings[field.key])));
    body.append(h("section", { class: "sheet" }, h("h2", { class: "letterpress" }, group.title), grid));
  }
  body.append(
    h(
      "section",
      { class: "sheet" },
      h("h2", { class: "letterpress" }, "密钥"),
      h(
        "p",
        {},
        settings.llm_api_key_set
          ? h("span", { class: "tag" }, "LLM 密钥已配置")
          : h("span", { class: "tag tag--alert" }, "LLM 密钥未配置"),
      ),
      h("p", { class: "muted" }, "密钥不会通过接口回显，也不会写进 data/settings.json —— 只改项目根目录的 .env。"),
    ),
  );

  const save = h(
    "button",
    {
      class: "btn btn-primary",
      onClick: async () => {
        save.disabled = true;
        try {
          const result = await api.saveSettings(collect(body));
          dirty = false;
          toast("已保存；已导出的章节需要到书页点重渲染");
          status.textContent = `已在界面上改过：${(result.overlay_keys || []).join("、")}`;
        } catch (error) {
          toast(error.message, "error");
        } finally {
          save.disabled = false;
        }
      },
    },
    "保存设置",
  );
  body.addEventListener("change", () => {
    dirty = true;
  });
  window.addEventListener("beforeunload", (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
  return h(
    "div",
    {},
    h(
      "div",
      { class: "page-head" },
      h("div", { class: "page-head__title" }, h("h1", { class: "letterpress" }, "设置"), h("p", { class: "muted" }, "这里的改动写进 data/settings.json，重启 worker 后对新的任务生效。")),
      h("div", { class: "page-head__actions" }, save),
    ),
    status,
    body,
  );
}

export function render(host) {
  return renderWithState(host, () => build());
}
