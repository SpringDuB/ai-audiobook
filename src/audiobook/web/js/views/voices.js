import { api } from "../api.js";
import { emptyState, h, renderWithState, seal, toast } from "../ui.js";

function voiceCard(voice) {
  return h(
    "article",
    { class: "sheet voice-card" },
    h("div", { class: "row" }, seal(voice.name), h("span", { class: "letterpress" }, voice.name)),
    h(
      "div",
      { class: "voice-card__tags" },
      voice.gender ? h("span", { class: "tag" }, voice.gender) : null,
      voice.age_group ? h("span", { class: "tag" }, voice.age_group) : null,
      ...(voice.tags || []).slice(0, 4).map((tag) => h("span", { class: "tag" }, tag)),
    ),
    voice.has_ref
      ? h("audio", { controls: true, preload: "none", src: voice.sample_url })
      : h("p", { class: "muted" }, "缺少参考音频"),
  );
}

async function matrix(books) {
  if (!books.length) return emptyState("还没有书", "先导入书稿，才能给角色配音色。");
  let bookId = books[0].id;
  const holder = h("div");
  const draw = async () => {
    holder.replaceChildren(h("p", { class: "muted" }, "读取选角…"));
    try {
      const payload = await api.casting(bookId);
      const options = payload.voices?.length ? payload.voices : [{ id: payload.narrator_voice || "default", name: "default（未配置音色库）" }];
      holder.replaceChildren(
        h(
          "div",
          { class: "table-wrap" },
          h(
            "table",
            { class: "table" },
            h(
              "thead",
              {},
              h("tr", {}, h("th", {}, "角色"), h("th", {}, "当前音色"), h("th", {}, "匹配分"), h("th", {}, "原因"), h("th", {}, "改为")),
            ),
            h(
              "tbody",
              {},
              ...(payload.roles || []).map((role) => {
                const select = h(
                  "select",
                  {},
                  ...options.map((voice) =>
                    h("option", { value: voice.id, selected: voice.id === role.voice_id }, voice.name),
                  ),
                );
                return h(
                  "tr",
                  {},
                  h("td", {}, h("span", { class: "row" }, seal(role.name), role.name)),
                  h("td", { class: "mono" }, role.voice_name || role.voice_id || "—"),
                  h("td", { class: "mono" }, role.score ?? "—"),
                  h("td", { class: "muted" }, (role.reasons || []).join("；") || "—"),
                  h(
                    "td",
                    { class: "row" },
                    select,
                    h(
                      "button",
                      {
                        class: "btn btn-sm",
                        onClick: async () => {
                          try {
                            const result = await api.setCasting(bookId, role.role_id, { voice_id: select.value });
                            const chapters = result.invalidated || [];
                            toast(chapters.length ? `已保存；第 ${chapters.join("、")} 章成品已失效` : "已保存");
                          } catch (error) {
                            toast(error.message, "error");
                          }
                        },
                      },
                      "保存",
                    ),
                  ),
                );
              }),
            ),
          ),
        ),
        payload.voices?.length
          ? null
          : h("p", { class: "muted" }, "音色库为空：可以在设置页配置 TTS，M6 迁移后会带 96 个内置音色。"),
      );
    } catch (error) {
      holder.replaceChildren(h("div", { class: "error-block" }, `读取失败：${error.message}`));
    }
  };
  const picker = h(
    "select",
    {
      onChange: (event) => {
        bookId = event.target.value;
        draw();
      },
    },
    ...books.map((book) => h("option", { value: book.id }, book.title || book.id)),
  );
  await draw();
  return h("section", { class: "sheet" }, h("div", { class: "row row--between" }, h("h2", { class: "letterpress" }, "角色 → 音色"), picker), holder);
}

async function build() {
  const [voicesPayload, booksPayload] = await Promise.all([api.voices(), api.books()]);
  const voices = voicesPayload.voices || [];
  const books = booksPayload.books || [];
  const container = h(
    "div",
    {},
    h(
      "div",
      { class: "page-head" },
      h("div", { class: "page-head__title" }, h("h1", { class: "letterpress" }, "音色库"), h("p", { class: "muted" }, "内置与上传的音色都在这里；试听用的是参考音频。")),
      h("div", { class: "page-head__actions" }, h("a", { class: "btn btn-ghost", href: "#/settings" }, "TTS 设置")),
    ),
  );
  container.append(
    voices.length
      ? h("div", { class: "voice-grid" }, ...voices.map(voiceCard))
      : emptyState("音色库是空的", "把参考音频放进 data/voices/<id>/（ref.wav + voice.json），或等 M6 迁移内置音色。"),
  );
  container.append(await matrix(books));
  return container;
}

export function render(host) {
  return renderWithState(host, () => build());
}
