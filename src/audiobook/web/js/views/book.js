import { api } from "../api.js";
import { duration, shortId, stateLabel } from "../format.js";
import { emptyState, h, renderWithState, toast } from "../ui.js";

function chapterRow(chapter, bookId) {
  return h(
    "tr",
    {},
    h("td", { class: "mono" }, chapter.index),
    h("td", {}, h("a", { href: `#/book/${bookId}/chapter/${chapter.index}` }, chapter.title)),
    h("td", { class: "mono" }, chapter.chars ?? "—"),
    h("td", { class: "mono" }, chapter.scenes),
    h("td", { class: "mono" }, chapter.lines),
    h("td", { class: "mono" }, chapter.duration_sec ? duration(chapter.duration_sec) : "—"),
    h("td", {}, h("span", { class: "state-tag", dataset: { state: chapter.state } }, stateLabel(chapter.state))),
    h("td", {}, h("a", { class: "btn btn-sm", href: `#/book/${bookId}/chapter/${chapter.index}` }, "校对")),
  );
}

async function build(route, host) {
  const [detail, chaptersPayload] = await Promise.all([api.book(route.bookId), api.chapters(route.bookId)]);
  const chapters = chaptersPayload.chapters || [];
  const stats = chapters.reduce(
    (acc, chapter) => {
      acc.lines += chapter.lines;
      acc.generated += chapter.state === "rendered" ? 1 : 0;
      acc.duration += chapter.duration_sec || 0;
      return acc;
    },
    { lines: 0, generated: 0, duration: 0 },
  );
  const title = detail.book?.title || route.bookId;
  const refresh = () => render(host, route);
  const runButton = h(
    "button",
    {
      class: "btn btn-primary",
      onClick: async (event) => {
        event.target.disabled = true;
        try {
          const result = await api.runBook(route.bookId);
          toast(result.queued ? `已入队 ${result.queued} 个任务` : "没有需要排队的任务");
        } catch (error) {
          toast(error.message, "error");
        } finally {
          event.target.disabled = false;
          refresh();
        }
      },
    },
    "继续生成",
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
        h("p", { class: "muted" }, h("a", { href: "#/shelf" }, "书架"), " / ", shortId(route.bookId)),
        h("h1", { class: "letterpress" }, title),
      ),
      h(
        "div",
        { class: "page-head__actions" },
        runButton,
        h(
          "button",
          {
            class: "btn",
            onClick: async () => {
              try {
                const result = await api.exportBook(route.bookId, { mode: "all" });
                toast("已入队整本导出，去任务中心看进度");
                window.location.hash = "#/jobs";
              } catch (error) {
                toast(error.message, "error");
              }
            },
          },
          "导出成品",
        ),
        h("a", { class: "btn btn-ghost", href: `#/book/${route.bookId}/issues` }, "异常清单"),
      ),
    ),
    h(
      "p",
      { class: "stat-line mono" },
      `章节 ${chapters.length}`,
      `句子 ${stats.lines}`,
      `已出成品 ${stats.generated}/${chapters.length}`,
      `总时长 ${duration(stats.duration)}`,
    ),
  );
  if (!chapters.length) {
    container.append(emptyState("还没有章节", "回到书架点一键分析，或用 aiab import 导入 txt。"));
    return container;
  }
  container.append(
    h(
      "div",
      { class: "table-wrap sheet" },
      h(
        "table",
        { class: "table" },
        h(
          "thead",
          {},
          h(
            "tr",
            {},
            h("th", {}, "#"),
            h("th", {}, "标题"),
            h("th", {}, "字数"),
            h("th", {}, "场景"),
            h("th", {}, "句子"),
            h("th", {}, "时长"),
            h("th", {}, "状态"),
            h("th", {}, "操作"),
          ),
        ),
        h("tbody", {}, ...chapters.map((chapter) => chapterRow(chapter, route.bookId))),
      ),
    ),
  );
  return container;
}

export function render(host, route) {
  return renderWithState(host, () => build(route, host));
}
