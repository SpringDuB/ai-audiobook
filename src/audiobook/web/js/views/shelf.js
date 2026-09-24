import { api } from "../api.js";
import { duration, stateLabel } from "../format.js";
import { emptyState, h, renderWithState, toast } from "../ui.js";

const RUN_LABELS = {
  empty: "一键分析",
  analyzing: "继续分析",
  analyzed: "一键生成",
  synthesizing: "继续生成",
  ready: "重新生成",
};

function statText(stats) {
  const parts = [`第 ${stats.chapters} 章`, `已分析 ${stats.analyzed}/${stats.chapters}`];
  if (stats.duration_sec) parts.push(`已生成 ${stats.generated}/${stats.chapters} · ${duration(stats.duration_sec)}`);
  else parts.push(`已生成 ${stats.generated}/${stats.chapters}`);
  return parts.join(" · ");
}

function bookCard(book, refresh) {
  const stats = book.stats || {};
  return h(
    "article",
    { class: "sheet book-card" },
    h(
      "div",
      { class: "row row--between" },
      h("a", { class: "letterpress book-card__title", href: `#/book/${book.id}` }, book.title || book.id),
      h("span", { class: "state-tag", dataset: { state: stats.state || "empty" } }, stateLabel(stats.state)),
    ),
    h("p", { class: "book-card__stats mono" }, statText(stats)),
    stats.issues
      ? h("span", { class: "tag tag--alert" }, `异常 ${stats.issues} 处`)
      : h("span", { class: "tag" }, "无异常"),
    h(
      "div",
      { class: "book-card__actions" },
      h(
        "button",
        {
          class: "btn btn-primary",
          onClick: async (event) => {
            event.target.disabled = true;
            try {
              const result = await api.runBook(book.id);
              toast(result.queued ? `已入队 ${result.queued} 个任务` : "没有需要排队的任务");
            } catch (error) {
              toast(error.message, "error");
            } finally {
              event.target.disabled = false;
              refresh();
            }
          },
        },
        RUN_LABELS[stats.state] || "一键分析",
      ),
      h("a", { class: "btn", href: `#/book/${book.id}` }, "打开"),
      h("a", { class: "btn btn-ghost", href: `#/book/${book.id}/issues` }, "异常"),
    ),
  );
}

function importForm(refresh) {
  const file = h("input", { type: "file", accept: ".txt", required: true });
  const title = h("input", { type: "text", placeholder: "书名（可选，默认用文件名）" });
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, "导入并分章");
  const form = h(
    "form",
    {
      class: "sheet import",
      onSubmit: async (event) => {
        event.preventDefault();
        const picked = file.files?.[0];
        if (!picked) {
          toast("先选一个 txt 文件", "error");
          return;
        }
        submit.disabled = true;
        try {
          const result = await api.upload(picked, title.value.trim() || picked.name.replace(/\.txt$/i, ""));
          toast("已导入，正在排队分章");
          file.value = "";
          title.value = "";
          await refresh();
          window.location.hash = `#/book/${result.book_id}`;
        } catch (error) {
          toast(error.message, "error");
        } finally {
          submit.disabled = false;
        }
      },
    },
    h("div", { class: "field" }, h("label", {}, "书稿（txt）"), file),
    h("div", { class: "field" }, h("label", {}, "书名"), title),
    submit,
  );
  return form;
}

async function build(host) {
  const container = h("div");
  const refresh = () => render(host, { name: "shelf" });
  const { books } = await api.books();
  container.append(
    h(
      "div",
      { class: "page-head" },
      h("div", { class: "page-head__title" }, h("h1", { class: "letterpress" }, "书架"), h("p", { class: "muted" }, "导入书稿 → 一键分析 → 一键生成。中途不需要守着浏览器。")),
      h("div", { class: "page-head__actions" }, h("a", { class: "btn", href: "#/jobs" }, "任务中心"), h("a", { class: "btn btn-ghost", href: "#/settings" }, "设置")),
    ),
    importForm(refresh),
  );
  if (!books.length) {
    container.append(emptyState("还没有书稿", "选一份 txt 导入，系统会自动清洗与分章。"));
  } else {
    container.append(h("div", { class: "book-grid" }, ...books.map((book) => bookCard(book, refresh))));
  }
  return container;
}

export function render(host) {
  return renderWithState(host, () => build(host));
}
