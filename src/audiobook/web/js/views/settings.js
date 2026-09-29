import { api } from "../api.js";
import { h, onTeardown, renderWithState, toast } from "../ui.js";

const LLM_FIELDS = [
  { key: "llm_base_url", label: "LLM 端点", type: "text" },
  { key: "llm_model", label: "模型", type: "text" },
  { key: "llm_temperature", label: "温度", type: "number", step: "0.1", min: "0", max: "2" },
  { key: "llm_concurrency", label: "并发", type: "number", min: "1", hint: "分析角色文本用；按你的额度给，8–16 稳" },
];

// 文本描述情绪通道（QwenEmotion）暂时关闭：后端 EMOTION_TEXT_ENABLED 打开时这里才会渲染
const EMOTION_FIELD = {
  key: "emotion_mode",
  label: "情绪控制",
  type: "select",
  options: [
    ["text", "文本描述（一句“怎么演”）"],
    ["vector", "8 维向量"],
  ],
  hint: "文本描述要服务端多加载 QwenEmotion（约 1.2GB 显存、每句多约 1.8 秒）；切换后需重新启动 TTS 服务",
};

const LAUNCH_FIELDS = [
  { key: "tts_backend", label: "推理后端", type: "select", options: [["indextts", "indextts（IndexTTS-2.5）"]] },
  {
    key: "tts_model_source",
    label: "模型来源",
    type: "select",
    options: [
      ["local", "local（本地已有权重，不下载）"],
      ["modelscope", "modelscope（首次启动自动下载）"],
      ["huggingface", "huggingface（首次启动自动下载）"],
    ],
  },
  { key: "tts_model_dir", label: "模型目录", type: "text", hint: "相对 tts/ 目录；local 时指向已有的 IndexTTS-2.5 权重" },
  { key: "tts_hf_endpoint", label: "HF 镜像（可空）", type: "text", hint: "例如 https://hf-mirror.com" },
  { key: "tts_port", label: "端口", type: "number", min: "1024" },
];

const ADVANCED_FIELDS = [
  { key: "tts_endpoints", label: "TTS 端点（一行一个）", type: "list" },
  { key: "synth_concurrency", label: "合成并发", type: "number", min: "1" },
  { key: "synth_concurrency_max", label: "并发上限", type: "number", min: "1" },
];

function fieldNode(field, value) {
  const holder = h("div", { class: "field" });
  holder.append(h("label", { for: `f-${field.key}` }, field.label));
  if (field.type === "select") {
    holder.append(
      h(
        "select",
        { id: `f-${field.key}`, dataset: { key: field.key } },
        ...field.options.map(([optionValue, label]) =>
          h("option", { value: optionValue, selected: String(value) === optionValue }, label),
        ),
      ),
    );
  } else if (field.type === "list") {
    holder.append(
      h("textarea", { id: `f-${field.key}`, rows: 2, dataset: { key: field.key }, value: (value || []).join("\n") }),
    );
  } else {
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
  }
  if (field.hint) holder.append(h("p", { class: "field__hint muted" }, field.hint));
  return holder;
}

function collect(scope, keys = null) {
  const patch = {};
  for (const node of scope.querySelectorAll("[data-key]")) {
    const key = node.dataset.key;
    if (keys && !keys.includes(key)) continue;
    if (node.tagName === "TEXTAREA") patch[key] = node.value.split("\n").map((line) => line.trim()).filter(Boolean);
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

  body.append(
    h(
      "section",
      { class: "setting-section" },
      h("h2", {}, "大模型（LLM）"),
      h("p", { class: "setting-section__note" }, "密钥只从项目根目录 .env 读，不回显、也不写进 settings.json。"),
      h("div", { class: "field-grid" }, ...LLM_FIELDS.map((field) => fieldNode(field, settings[field.key]))),
      h(
        "div",
        { class: "row" },
        settings.llm_api_key_set
          ? h("span", { class: "tag tag--ok" }, "密钥已配置")
          : h("span", { class: "tag tag--danger" }, "密钥未配置"),
        h("span", { class: "field__hint" }, "密钥只从 .env 读，界面上改不了"),
      ),
    ),
  );

  const statusLine = h("div", { class: "tts-status" });
  const logBox = h("pre", { class: "log" }, "（日志会在这里滚动）");
  const launchFields = [...LAUNCH_FIELDS];
  if (payload.emotion_text_enabled) launchFields.splice(1, 0, EMOTION_FIELD);
  const launchGrid = h(
    "div",
    { class: "field-grid field-grid--3" },
    ...launchFields.map((field) => fieldNode(field, settings[field.key])),
  );

  // 只剩 indextts；这里要说清楚"点下去会发生什么"，免得用户以为它不干活
  const backendHint = h("p", { class: "field__hint muted" });
  const backendSelect = launchGrid.querySelector("#f-tts_backend");
  const paintBackendHint = () => {
    backendHint.textContent =
      "启动时会先按「模型来源」下载/校验权重，再加载模型；进度和报错都打在下面的运行日志里。";
  };
  backendSelect.addEventListener("change", paintBackendHint);
  paintBackendHint();
  backendSelect.closest(".field").append(backendHint);

  const syncAdvanced = async () => {
    try {
      const fresh = (await api.settings()).settings || {};
      const engineSelect = body.querySelector("#f-engine");
      if (engineSelect) engineSelect.value = fresh.engine || "http";
      const endpoints = body.querySelector("#f-tts_endpoints");
      if (endpoints) endpoints.value = (fresh.tts_endpoints || []).join("\n");
    } catch {
      /* 高级区同步失败不影响主流程 */
    }
  };

  const startButton = h(
    "button",
    {
      class: "btn btn-primary",
      type: "button",
      dataset: { action: "tts-start" },
      onClick: async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        button.textContent = "启动中…";
        try {
          const result = await api.ttsStart(collect(launchGrid));
          toast(`TTS 已拉起：${result.service.url}；合成引擎已切到 http`);
          await poll();
          await syncAdvanced();
        } catch (error) {
          toast(error.message, "error");
        } finally {
          button.disabled = false;
          button.textContent = "一键启动 TTS 服务";
        }
      },
    },
    "一键启动 TTS 服务",
  );
  const stopButton = h(
    "button",
    {
      class: "btn btn-danger",
      type: "button",
      dataset: { action: "tts-stop" },
      onClick: async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          await api.ttsStop();
          toast("已停止本机 TTS 服务");
          await poll();
        } catch (error) {
          toast(error.message, "error");
        } finally {
          button.disabled = false;
        }
      },
    },
    "停止",
  );

  body.append(
    h(
      "section",
      { class: "setting-section" },
      h("h2", {}, "TTS 服务"),
      h(
        "p",
        { class: "setting-section__note" },
        "点一下，IndexTTS-2.5 就在本机独立进程里起来（和后端不共进程），合成引擎自动切过去。首次启动可能要等权重下载。",
      ),
      h("div", { class: "status-panel" }, statusLine),
      h("div", { class: "row", style: { marginBlock: "var(--space-3)" } }, startButton, stopButton),
      launchGrid,
      h("details", { class: "log-details" }, h("summary", {}, "运行日志"), logBox),
    ),
  );

  const advancedGrid = h(
    "div",
    { class: "field-grid field-grid--3" },
    ...ADVANCED_FIELDS.map((field) => fieldNode(field, settings[field.key])),
  );
  advancedGrid.querySelector("#f-tts_endpoints")?.closest(".field")?.classList.add("field--wide");
  body.append(
    h(
      "details",
      { class: "panel advanced" },
      h("summary", {}, "高级（一般不用动）"),
      h(
        "div",
        { class: "panel__body" },
        h("p", { class: "field__hint" }, "改完保存即可：worker 每轮任务前会重读设置，不用重启 worker。"),
        advancedGrid,
        h("p", { class: "field__hint" }, "ffmpeg 由项目自带（uv sync 时就装好）；响度按有声书的稳妥默认值固定，不用你调。"),
        h("p", { class: "field__hint" }, "合成引擎固定为 http（连独立 TTS 服务），由「一键启动」自动配置端点。"),
      ),
    ),
  );

  const status = h(
    "p",
    { class: "field__hint" },
    saved.size ? `已在界面上改过：${[...saved].join("、")}` : "目前全部来自 .env / 环境变量",
  );
  const save = h(
    "button",
    {
      class: "btn btn-primary",
      type: "button",
      onClick: async () => {
        save.disabled = true;
        try {
          const result = await api.saveSettings(collect(body));
          toast("已保存；worker 下一轮任务会用到新值");
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

  let logOffset = 0;
  const paintStatus = (info) => {
    const service = info.service || {};
    const state = service.healthy ? "running" : service.running ? "starting" : "idle";
    const label = service.healthy ? "运行中" : service.running ? "启动中（加载模型…）" : "未运行";
    statusLine.replaceChildren(
      ...[
        h("span", { class: "dot", dataset: { state } }),
        h("span", { class: label === "未运行" ? "muted" : "" }, label),
        h("span", { class: "mono muted" }, service.url || ""),
        h("span", { class: "mono muted" }, `backend ${service.backend || settings.tts_backend}`),
        service.pid ? h("span", { class: "mono muted" }, `pid ${service.pid}`) : null,
        info.engine ? h("span", { class: "mono muted" }, `合成引擎 ${info.engine}`) : null,
        service.running && service.backend !== "indextts"
          ? h("span", { class: "tag tag--alert" }, "跑着的不是 indextts，点「一键启动」会重启它")
          : null,
      ].filter(Boolean),
    );
  };
  const poll = async () => {
    try {
      const info = await api.ttsLocal();
      paintStatus(info);
      const logs = await api.ttsLogs(logOffset);
      logOffset = logs.offset || 0;
      if (logs.reset) logBox.textContent = "";
      if (logs.lines?.length) {
        logBox.textContent = `${logBox.textContent}\n${logs.lines.join("\n")}`
          .split("\n")
          .slice(-220)
          .join("\n");
        logBox.scrollTop = logBox.scrollHeight;
      }
    } catch (error) {
      statusLine.replaceChildren(h("span", { class: "tag tag--alert" }, `状态读取失败：${error.message}`));
    }
  };
  await poll();
  const timer = setInterval(poll, 2500);
  onTeardown(() => clearInterval(timer));

  return h(
    "div",
    {},
    h(
      "div",
      { class: "page-head" },
      h(
        "div",
        { class: "page-head__title" },
        h("h1", { class: "letterpress" }, "设置"),
        h("p", { class: "muted" }, "改动写进 data/settings.json；worker 每轮任务前重读，不用重启。"),
      ),
      h("div", { class: "page-head__actions" }, save),
    ),
    status,
    h("div", { class: "settings-grid" }, body),
  );
}

export function render(host) {
  return renderWithState(host, () => build());
}
