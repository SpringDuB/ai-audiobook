// 悬浮音色选择器：搜索 + 分类（性别/年龄/用途）+ 参考音频试听。
// 书架、音色库、选角面板共用这一份。

import { api } from "./api.js";
import { h, toast } from "./ui.js";

let library = null;
let openPanel = null;

export async function loadVoices({ force = false } = {}) {
  if (library && !force) return library;
  const payload = await api.voices();
  library = payload.voices || [];
  return library;
}

function unique(values) {
  return [...new Set(values.filter(Boolean))];
}

function facets(voices) {
  return {
    gender: unique(voices.map((voice) => voice.gender)),
    age: unique(voices.map((voice) => voice.age_group)),
    usage: unique(voices.flatMap((voice) => voice.usage_type || [])),
  };
}

function haystack(voice) {
  return [
    voice.name,
    voice.id,
    voice.description,
    voice.gender,
    voice.age_group,
    voice.speech_rate ? `语速${voice.speech_rate}` : "",
    ...(voice.tags || []),
    ...(voice.personality || []),
    ...(voice.genres || []),
    ...(voice.mood || []),
    ...(voice.voice_quality || []),
    ...(voice.usage_type || []),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

export function closeVoicePicker() {
  if (openPanel) {
    openPanel.remove();
    openPanel = null;
  }
}

function chip(label, active, onClick, className = "chip") {
  return h("button", { class: className, type: "button", "aria-pressed": String(active), onClick }, label);
}

function place(node, anchor) {
  const rect = anchor.getBoundingClientRect();
  const width = Math.min(460, window.innerWidth - 24);
  node.style.width = `${width}px`;
  const left = rect.left + rect.width / 2 < window.innerWidth / 2 ? rect.right + 10 : rect.left - width - 10;
  node.style.left = `${Math.max(12, Math.min(left, window.innerWidth - width - 12))}px`;
  const height = node.offsetHeight || 420;
  node.style.top = `${Math.max(12, Math.min(rect.top, window.innerHeight - height - 12))}px`;
}

export async function openVoicePicker({ anchor, roleName = "", currentVoiceId = "", onPick }) {
  closeVoicePicker();
  let voices;
  try {
    voices = await loadVoices();
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  if (!voices.length) {
    toast("音色库是空的：先迁移内置音色或放进 data/voices/", "error");
    return;
  }
  const groups = facets(voices);
  const filter = { q: "", gender: null, age: null, usage: null };
  const list = h("div", { class: "picker__list" });
  const audio = new Audio();
  audio.preload = "none";
  let playing = null;

  const paint = () => {
    const needle = filter.q.trim().toLowerCase();
    const matched = voices.filter((voice) => {
      if (filter.gender && voice.gender !== filter.gender) return false;
      if (filter.age && voice.age_group !== filter.age) return false;
      if (filter.usage && !(voice.usage_type || []).includes(filter.usage)) return false;
      return !needle || haystack(voice).includes(needle);
    });
    list.replaceChildren(
      h("p", { class: "picker__count mono" }, `${matched.length} / ${voices.length} 个音色`),
      ...(matched.length
        ? matched.map((voice) => voiceRow(voice))
        : [h("p", { class: "muted" }, "没有符合条件的音色，换个筛选试试。")]),
    );
  };

  const play = (voice, button) => {
    if (playing === voice.id) {
      audio.pause();
      playing = null;
      button.textContent = "试听";
      return;
    }
    if (!voice.has_ref) {
      toast("这个音色缺少参考音频", "error");
      return;
    }
    audio.src = voice.sample_url;
    audio.play().catch(() => toast("浏览器拦住了播放，再点一次", "error"));
    playing = voice.id;
    button.textContent = "停止";
  };

  const voiceRow = (voice) =>
    h(
      "article",
      { class: voice.id === currentVoiceId ? "picker__row is-current" : "picker__row" },
      h(
        "button",
        {
          class: "picker__pick",
          type: "button",
          onClick: () => {
            closeVoicePicker();
            Promise.resolve(onPick?.(voice)).catch((error) => toast(error.message, "error"));
          },
        },
        h(
          "div",
          { class: "picker__pick-head" },
          h("span", { class: "picker__name letterpress" }, voice.name),
          h("span", { class: "mono muted" }, voice.id),
        ),
        h(
          "div",
          { class: "picker__tags" },
          voice.gender ? h("span", { class: "tag" }, voice.gender) : null,
          voice.age_group ? h("span", { class: "tag" }, voice.age_group) : null,
          ...(voice.usage_type || []).map((usage) => h("span", { class: "tag" }, usage)),
          ...(voice.voice_quality || []).slice(0, 2).map((tag) => h("span", { class: "tag" }, tag)),
        ),
        voice.description ? h("p", { class: "picker__desc muted" }, voice.description) : null,
      ),
      h(
        "div",
        { class: "picker__row-actions" },
        h(
          "button",
          {
            class: "btn btn-sm",
            type: "button",
            onClick: (event) => play(voice, event.currentTarget),
          },
          "试听",
        ),
      ),
    );

  const search = h("input", {
    type: "search",
    placeholder: "搜名字、性格、风格、用途…",
    oninput: (event) => {
      filter.q = event.target.value;
      paint();
    },
  });
  const facetRow = (label, key, values) =>
    values.length
      ? h(
          "div",
          { class: "picker__facet" },
          h("span", { class: "picker__facet-label mono" }, label),
          h(
            "div",
            { class: "picker__chips" },
            chip("全部", filter[key] === null, () => {
              filter[key] = null;
              refreshFacets();
            }),
            ...values.map((value) =>
              chip(value, filter[key] === value, () => {
                filter[key] = filter[key] === value ? null : value;
                refreshFacets();
              }),
            ),
          ),
        )
      : null;

  const facetHolder = h("div", { class: "picker__facets" });
  const refreshFacets = () => {
    facetHolder.replaceChildren(
      facetRow("性别", "gender", groups.gender),
      facetRow("年龄", "age", groups.age),
      facetRow("用途", "usage", groups.usage),
    );
    paint();
  };
  refreshFacets();

  const panel = h(
    "div",
    { class: "picker", role: "dialog", "aria-label": "选择音色" },
    h(
      "header",
      { class: "picker__head" },
      h("h3", { class: "letterpress" }, roleName ? `给「${roleName}」选音色` : "选择音色"),
      h("button", { class: "btn btn-sm btn-ghost", type: "button", onClick: closeVoicePicker }, "关闭"),
    ),
    h("div", { class: "picker__search" }, search),
    facetHolder,
    list,
  );
  const backdrop = h("div", {
    class: "picker__backdrop",
    onClick: closeVoicePicker,
  });
  const host = h("div", { class: "picker-host" }, backdrop, panel);
  document.body.append(host);
  openPanel = host;
  place(panel, anchor);
  search.focus();
  const onKey = (event) => {
    if (event.key === "Escape") closeVoicePicker();
  };
  document.addEventListener("keydown", onKey);
  const observer = new MutationObserver(() => {
    if (!document.body.contains(host)) {
      document.removeEventListener("keydown", onKey);
      observer.disconnect();
      audio.pause();
      openPanel = null;
    }
  });
  observer.observe(document.body, { childList: true });
}
