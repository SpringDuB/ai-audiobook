// 音色库：试听、上传新音色、停用/启用。
// 停用 = 收进页面底部的折叠区；大模型推荐和选音色悬浮窗都不会再看到它。

import { api } from "../api.js";
import { duration } from "../format.js";
import { emptyState, h, onTeardown, renderWithState, seal, toast } from "../ui.js";
import { loadVoices } from "../voicepicker.js";

const GENDERS = ["", "男", "女", "中性"];
const AGES = ["", "儿童", "少年", "青年", "中年", "老年"];
const RATES = ["", "快", "中", "慢"];
const USAGES = ["", "角色对话", "播报解说", "旁白"];

/* 试听：整页共用一个 audio，卡片上的播放条只做外观 */

const preview = { audio: null, ui: null };

function resetPreview() {
  const ui = preview.ui;
  if (ui) {
    ui.button.textContent = "▶";
    ui.fill.style.width = "0%";
  }
  preview.ui = null;
}

function previewAudio() {
  if (preview.audio) return preview.audio;
  const audio = new Audio();
  audio.preload = "none";
  audio.addEventListener("timeupdate", () => {
    const ui = preview.ui;
    if (!ui || !audio.duration) return;
    ui.fill.style.width = `${Math.min(100, (audio.currentTime / audio.duration) * 100)}%`;
    ui.time.textContent = duration(Math.max(0, audio.duration - audio.currentTime));
  });
  audio.addEventListener("ended", resetPreview);
  preview.audio = audio;
  return audio;
}

function playVoice(voice, ui) {
  const audio = previewAudio();
  if (preview.ui === ui && !audio.paused) {
    audio.pause();
    resetPreview();
    return;
  }
  if (preview.ui && preview.ui !== ui) resetPreview();
  preview.ui = ui;
  audio.src = voice.sample_url;
  audio
    .play()
    .then(() => {
      ui.button.textContent = "■";
    })
    .catch(() => {
      resetPreview();
      toast("浏览器拦住了播放，再点一次", "error");
    });
}

function playerFor(voice) {
  const fill = h("span", { class: "player__fill", style: { width: "0%" } });
  const time = h("span", { class: "player__time" }, "--:--");
  const button = h(
    "button",
    { class: "player__btn", type: "button", "aria-label": `试听 ${voice.name}` },
    "▶",
  );
  button.addEventListener("click", () => playVoice(voice, { button, fill, time }));
  return h(
    "span",
    { class: "player", role: "group", "aria-label": `试听 ${voice.name}` },
    button,
    h("span", { class: "player__track" }, fill),
    time,
  );
}

/* ------------------------------------------------------------------ 表单 */

function pick(options, value = "") {
  return h(
    "select",
    {},
    ...options.map((item) => h("option", { value: item, selected: item === value }, item || "不填")),
  );
}

function field(label, control, hint, id = null) {
  return h(
    "div",
    { class: "field" },
    h("label", { for: id }, label),
    control,
    hint ? h("p", { class: "field__hint muted" }, hint) : null,
  );
}

function uploadForm(refresh) {
  const name = h("input", { id: "voice-name", type: "text", placeholder: "例如：低沉反派…", required: true });
  const file = h("input", {
    id: "voice-file",
    type: "file",
    accept: "audio/*,.wav,.mp3,.m4a,.flac,.ogg",
    required: true,
  });
  const gender = pick(GENDERS);
  const age = pick(AGES);
  const rate = pick(RATES);
  const usage = pick(USAGES, "角色对话");
  const tags = h("input", { id: "voice-tags", type: "text", placeholder: "磁性, 低沉, 反派…" });
  const description = h("textarea", {
    id: "voice-desc",
    rows: 2,
    placeholder: "一句话介绍这个音色，用来给大模型推荐…",
  });
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, "加入音色库");

  return h(
    "form",
    {
      class: "panel voice-upload",
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
    h(
      "div",
      { class: "panel__bar" },
      h("h2", {}, "上传新音色"),
      h("span", { class: "count" }, "参考音频 5 到 15 秒最稳"),
    ),
    h(
      "div",
      { class: "panel__body" },
      h(
        "div",
        { class: "upload" },
        h(
          "label",
          { class: "dropzone--audio", for: "voice-file" },
          h("span", {}, "把参考音频拖到这里"),
          h("span", { class: "btn btn-sm" }, "选择文件"),
          h("span", { class: "field__hint" }, "wav / mp3 / m4a / flac / ogg，超过 60 秒会被拒"),
          file,
        ),
        h(
          "div",
          { class: "field-grid" },
          field("音色名称", name, null, "voice-name"),
          field("性别", gender),
          field("年龄", age),
          field("语速", rate),
          field("用途", usage),
          field("标签", tags, null, "voice-tags"),
          h(
            "div",
            { class: "field field--wide" },
            h("label", { for: "voice-desc" }, "介绍"),
            description,
            h("p", { class: "field__hint muted" }, "标签和介绍都会进大模型的音色清单，写清性格与题材更容易被推荐。"),
          ),
        ),
      ),
      h(
        "div",
        { class: "row", style: { justifyContent: "flex-end" } },
        h("span", { class: "field__hint" }, "上传后立刻可以在选音色里看到"),
        submit,
      ),
    ),
  );
}

/* ------------------------------------------------------------------ 卡片 */

function voiceCard(voice, refresh) {
  const toggle = h(
    "button",
    {
      class: "btn btn-sm btn-danger",
      type: "button",
      title: "停用后大模型推荐和选音色都看不到它；音频保留，随时可再启用",
      onClick: async (event) => {
        event.target.disabled = true;
        try {
          await api.patchVoice(voice.id, { disabled: true });
          await loadVoices({ force: true });
          resetPreview();
          preview.audio?.pause();
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
    { class: "voice-card", dataset: { voiceId: voice.id } },
    h(
      "div",
      { class: "voice-card__head" },
      seal(voice.name),
      h("span", { class: "voice-card__name" }, voice.name),
      voice.source === "upload" ? h("span", { class: "tag tag--accent" }, "上传") : h("span", { class: "tag" }, "内置"),
    ),
    h(
      "div",
      { class: "voice-card__tags" },
      voice.gender ? h("span", { class: "tag" }, voice.gender) : null,
      voice.age_group ? h("span", { class: "tag" }, voice.age_group) : null,
      voice.speech_rate ? h("span", { class: "tag" }, voice.speech_rate) : null,
      ...(voice.tags || []).slice(0, 4).map((tag) => h("span", { class: "tag" }, tag)),
    ),
    voice.description ? h("p", { class: "voice-card__desc" }, voice.description) : null,
    voice.has_ref ? playerFor(voice) : h("p", { class: "field__hint" }, "缺少参考音频"),
    h(
      "div",
      { class: "voice-card__actions" },
      h("span", { class: "voice-card__id" }, voice.id),
      h("span", { class: "spacer" }),
      toggle,
    ),
  );
}

function disabledSection(voices, refresh) {
  return h(
    "details",
    { class: "panel voice-disabled" },
    h("summary", {}, `已停用（${voices.length}）`),
    h(
      "p",
      { class: "panel__body field__hint", style: { margin: "0" } },
      "停用的音色不会再被大模型推荐，也不会出现在选音色里；音频和标签都还在，点「启用」就恢复。",
    ),
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
  );
}

async function build(host) {
  const refresh = () => render(host, { name: "voices" });
  const payload = await api.voices({ includeDisabled: true });
  const voices = payload.voices || [];
  const active = voices.filter((voice) => !voice.disabled);
  const disabled = voices.filter((voice) => voice.disabled);

  onTeardown(() => {
    resetPreview();
    preview.audio?.pause();
  });

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
      h(
        "div",
        { class: "page-head__actions" },
        h("a", { class: "btn btn-ghost", href: "#/settings" }, "TTS 设置"),
      ),
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
