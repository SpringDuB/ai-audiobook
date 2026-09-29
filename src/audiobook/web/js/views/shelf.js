import { api } from "../api.js";
import { duration, stateLabel } from "../format.js";
import { icon } from "../icons.js";
import { confirmDialog, emptyState, h, renderWithState, toast } from "../ui.js";

function statText(stats) {
  const parts = [`第 ${stats.chapters} 章`, `已分析 ${stats.analyzed}/${stats.chapters}`];
  if (stats.duration_sec) parts.push(`已生成 ${stats.generated}/${stats.chapters} · ${duration(stats.duration_sec)}`);
  else parts.push(`已生成 ${stats.generated}/${stats.chapters}`);
  return parts.join(" · ");
}

function bookCard(book, refresh) {
  const stats = book.stats || {};
  const open = `#/book/${book.id}`;
  const remove = h(
    "button",
    {
      class: "btn btn-danger",
      type: "button",
      onClick: async (event) => {
        event.stopPropagation();   // 卡片整体可点，删除别顺手把书页也打开
        const button = event.currentTarget;
        const ok = await confirmDialog({
          title: "删除这本书？",
          message: `《${book.title || book.id}》的分析结果、已生成音频和字幕会一起删除，无法恢复。`,
          confirmLabel: "删除",
          danger: true,
        });
        if (!ok) return;
        button.disabled = true;
        try {
          const result = await api.deleteBook(book.id);
          const jobs = result?.removed_jobs || 0;
          toast(jobs ? `已删除，同时清掉 ${jobs} 个任务` : "已删除");
          refresh();
        } catch (error) {
          toast(error.message, "error");
          button.disabled = false;
        }
      },
    },
    icon("trash-2", { size: 13 }),
    "删除",
  );
  return h(
    "article",
    {
      class: "sheet book-card",
      role: "link",
      tabIndex: 0,
      "aria-label": `打开《${book.title || book.id}》`,
      onClick: (event) => {
        if (event.target.closest("a, button")) return;
        window.location.hash = open;
      },
      onKeydown: (event) => {
        if (event.target !== event.currentTarget) return;
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        window.location.hash = open;
      },
    },
    h(
      "div",
      { class: "book-card__top" },
      h("a", { class: "book-card__title", href: open, title: book.title || book.id }, book.title || book.id),
      h("span", { class: "state-tag", dataset: { state: stats.state || "empty" } }, stateLabel(stats.state)),
    ),
    h("p", { class: "book-card__stats mono" }, statText(stats)),
    stats.issues
      ? h("span", { class: "tag tag--alert" }, `异常 ${stats.issues} 处`)
      : h("span", { class: "tag" }, "无异常"),
    h(
      "div",
      { class: "book-card__actions" },
      h("a", { class: "btn btn-primary", href: open }, icon("book-open", { size: 13 }), "打开"),
      remove,
    ),
  );
}

function importForm(refresh) {
  const file = h("input", {
    id: "import-file",
    type: "file",
    accept: ".txt,.epub,application/epub+zip,text/plain",
    required: true,
  });
  // 原生 file 控件比文本框高一截，会把这一行的标签顶歪；包成和音色库同款的选择框
  const fileName = h("span", { class: "filepick__name" }, "未选择文件");
  file.addEventListener("change", () => {
    fileName.textContent = file.files?.[0]?.name || "未选择文件";
  });
  const picker = h(
    "label",
    { class: "filepick", for: "import-file" },
    h("span", { class: "btn btn-sm" }, icon("upload", { size: 12 }), "选择文件"),
    fileName,
    file,
  );
  const title = h("input", { type: "text", placeholder: "书名（可选，默认用文件名）" });
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, icon("upload", { size: 14 }), "导入并分章");
  const form = h(
    "form",
    {
      class: "sheet import",
      onSubmit: async (event) => {
        event.preventDefault();
        const picked = file.files?.[0];
        if (!picked) {
          toast("先选一个 txt / epub 文件", "error");
          return;
        }
        submit.disabled = true;
        try {
          const fallback = picked.name.replace(/\.(txt|epub)$/i, "");
          const result = await api.upload(picked, title.value.trim() || fallback);
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
    // 三个控件同一行：标签对齐标签、控件对齐控件；提示语独占下面一行，
    // 否则「书稿有提示、书名没有」会把两列的基线顶歪。
    h(
      "div",
      { class: "import__row" },
      h("div", { class: "field" }, h("label", { for: "import-file" }, "书稿（txt / epub）"), picker),
      h("div", { class: "field" }, h("label", {}, "书名"), title),
      submit,
    ),
    h("p", { class: "field__hint muted" }, "epub 会先抽成纯文本再分章，原文一样保留在书页里。"),
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
      h(
        "div",
        { class: "page-head__title" },
        h("h1", { class: "letterpress" }, "书架"),
        h("p", { class: "muted" }, "导入书稿（txt / epub）→ 打开书页分析台词 → 生成音频 → 导出成品。"),
      ),
      h(
        "div",
        { class: "page-head__actions" },
        h("a", { class: "btn", href: "#/jobs" }, icon("list-checks", { size: 14 }), "任务中心"),
        h("a", { class: "btn btn-ghost", href: "#/settings" }, icon("settings", { size: 14 }), "设置"),
      ),
    ),
    importForm(refresh),
  );
  if (!books.length) {
    container.append(emptyState("还没有书稿", "选一份 txt / epub 导入，系统会自动清洗与分章。"));
  } else {
    container.append(h("div", { class: "book-grid" }, ...books.map((book) => bookCard(book, refresh))));
  }
  return container;
}

export function render(host) {
  return renderWithState(host, () => build(host));
}
