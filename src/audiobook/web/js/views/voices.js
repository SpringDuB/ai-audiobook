// 音色库：试听、上传新音色、停用/启用。
// 停用 = 收进下面那一栏；大模型推荐和选音色悬浮窗都不会再看到它。

import { api } from "../api.js";
import { emptyState, h, renderWithState, seal, toast } from "../ui.js";
import { loadVoices } from "../voicepicker.js";

const GENDERS = ["", "男", "女", "中性"];
const AGES = ["", "儿童", "少年", "青年", "中年", "老年"];
const RATES = ["", "快", "中", "慢"];
const USAGES = ["", "角色对话", "播报解说", "旁白"];

function pick(options, value = "") {
  return h(
    "select",
    {},
    ...options.map((item) => h("option", { value: item, selected: item === value }, item || "不填")),
  );
}

function field(label, control, hint) {
  return h(
    "div",
    { class: "field" },
    h("label", {}, label),
    control,
    hint ? h("p", { class: "field__hint muted" }, hint) : null,
  );
}

function uploadForm(refresh) {
  const name = h("input", { type: "text", placeholder: "例如：低沉反派", required: true });
  const file = h("input", { type: "file", accept: "audio/*,.wav,.mp3,.m4a,.flac,.ogg", required: true });
  const gender = pick(GENDERS);
  const age = pick(AGES);
  const rate = pick(RATES);
  const usage = pick(USAGES, "角色对话");
  const tags = h("input", { type: "text", placeholder: "磁性, 低沉, 反派（逗号分隔，可空）" });
  const description = h("textarea", { rows: 2, placeholder: "一句话介绍这个音色，用来给大模型推荐（可空）" });
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, "加入音色库");

  return h(
    "form",
    {
      class: "sheet voice-upload",
      onSubmit: async (event) => {
        event.preventDefault();
        const picked = file.files?.[0];
        if (!picked) {
          toast("先选一个参考音频", "error");
          return;
        }
        const form = new FormData();
        form.append("name", name.value.trim());
        form.append("file", picked);
        form.append("gender", gender.value);
        form.append("age_group", age.value);
        form.append("speech_rate", rate.value);
        form.append("usage_type", usage.value);
        form.append("tags", tags.value);
        form.append("description", description.value);
        submit.disabled = true;
        try {
          const result = await api.uploadVoice(form);
          toast(`已加入音色库：${result.voice?.name || name.value.trim()}`);
          name.value = "";
          tags.value = "";
          description.value = "";
          file.value = "";
          await loadVoices({ force: true });   // 选音色悬浮窗立刻能看到
          refresh();
        } catch (error) {
          toast(error.message, "error");
        } finally {
          submit.disabled = false;
        }
      },
    },
    h("h2", { class: "letterpress" }, "上传新音色"),
    h(
      "div",
      { class: "voice-upload__grid" },
      field("音色名称", name),
      field("参考音频", file, "5–15 秒干净人声最好；wav / mp3 / m4a / flac / ogg 都行，超过 60 秒会被拒。"),
      field("性别", gender),
      field("年龄", age),
      field("语速", rate),
      field("用途", usage),
      field("标签", tags, "标签会进大模型的音色库清单，写清性格/题材更容易被推荐。"),
      field("介绍", description),
    ),
    h("div", { class: "row" }, submit),
  );
}

function voiceCard(voice, refresh) {
  const audio = h("audio", { controls: true, preload: "none", src: voice.sample_url });
  const toggle = h(
    "button",
    {
      class: "btn btn-sm",
      type: "button",
      title: "停用后大模型推荐和选音色都看不到它；音频保留，随时可再启用",
      onClick: async (event) => {
        event.target.disabled = true;
        try {
          await api.patchVoice(voice.id, { disabled: true });
          await loadVoices({ force: true });
          audio.pause();
          toast(`「${voice.name}」已停用`);
          refresh();
        } catch (error) {
          toast(error.message, "error");
          event.target.disabled = false;
        }
      },
    },
    "停用",
  );
  return h(
    "article",
    { class: "sheet voice-card", dataset: { voiceId: voice.id } },
    h(
      "div",
      { class: "row" },
      seal(voice.name),
      h("span", { class: "letterpress" }, voice.name),
      voice.source === "upload" ? h("span", { class: "tag" }, "上传") : null,
    ),
    h(
      "div",
      { class: "voice-card__tags" },
      voice.gender ? h("span", { class: "tag" }, voice.gender) : null,
      voice.age_group ? h("span", { class: "tag" }, voice.age_group) : null,
      ...(voice.tags || []).slice(0, 4).map((tag) => h("span", { class: "tag" }, tag)),
    ),
    voice.description ? h("p", { class: "voice-card__desc muted" }, voice.description) : null,
    voice.has_ref ? audio : h("p", { class: "muted" }, "缺少参考音频"),
    h("div", { class: "voice-card__actions" }, toggle),
  );
}

function disabledSection(voices, refresh) {
  return h(
    "section",
    { class: "sheet voice-disabled" },
    h("h2", { class: "letterpress" }, `已停用（${voices.length}）`),
    h("p", { class: "muted" }, "停用的音色不会再被大模型推荐，也不会出现在选音色里；音频和标签都还在，点「启用」就恢复。"),
    h(
      "div",
      { class: "voice-disabled__list" },
      ...voices.map((voice) =>
        h(
          "div",
          { class: "voice-disabled__row", dataset: { voiceId: voice.id } },
          seal(voice.name),
          h("span", { class: "voice-disabled__name" }, voice.name),
          h("span", { class: "mono muted" }, voice.id),
          h(
            "button",
            {
              class: "btn btn-sm",
              type: "button",
              onClick: async (event) => {
                event.target.disabled = true;
                try {
                  await api.patchVoice(voice.id, { disabled: false });
                  await loadVoices({ force: true });
                  toast(`「${voice.name}」已重新启用`);
                  refresh();
                } catch (error) {
                  toast(error.message, "error");
                  event.target.disabled = false;
                }
              },
            },
            "启用",
          ),
        ),
      ),
    ),
  );
}

async function build(host) {
  const refresh = () => render(host, { name: "voices" });
  const payload = await api.voices({ includeDisabled: true });
  const voices = payload.voices || [];
  const active = voices.filter((voice) => !voice.disabled);
  const disabled = voices.filter((voice) => voice.disabled);

  const container = h(
    "div",
    {},
    h(
      "div",
      { class: "page-head" },
      h(
        "div",
        { class: "page-head__title" },
        h("h1", { class: "letterpress" }, "音色库"),
        h(
          "p",
          { class: "muted" },
          `可用 ${active.length} 个${disabled.length ? ` · 已停用 ${disabled.length} 个` : ""}；试听用的是参考音频。`,
        ),
      ),
      h("div", { class: "page-head__actions" }, h("a", { class: "btn btn-ghost", href: "#/settings" }, "TTS 设置")),
    ),
    uploadForm(refresh),
  );
  container.append(
    active.length
      ? h("div", { class: "voice-grid" }, ...active.map((voice) => voiceCard(voice, refresh)))
      : emptyState("还没有可用音色", "用上面的表单传一段参考音频，或先跑一次内置音色迁移。"),
  );
  if (disabled.length) container.append(disabledSection(disabled, refresh));
  return container;
}

export function render(host) {
  return renderWithState(host, () => build(host));
}
