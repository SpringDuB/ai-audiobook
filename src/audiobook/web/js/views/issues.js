import { api } from "../api.js";
import { clock, issueKindLabel, shortId } from "../format.js";
import { icon } from "../icons.js";
import { emptyState, h, renderWithState, toast } from "../ui.js";

const FAIL_KINDS = new Set([
  "pass_a_failed",
  "pass_b_failed",
  "pass_c_failed",
  "chapter_analysis_failed",
  "chapter_window_failed",
  "tts_line_failed",
  "tts_endpoint_down",
  "cast_sheet_failed",
  "voice_design_failed",
  "render_duration_mismatch",
]);

function issueRow(issue) {
  return h(
    "article",
    { class: "issue-row", dataset: { severity: FAIL_KINDS.has(issue.kind) ? "fail" : "warn" } },
    h(
      "div",
      { class: "issue-row__head mono" },
      clock(issue.ts),
      issueKindLabel(issue.kind),
      issue.chapter === null || issue.chapter === undefined ? "全书" : `第${issue.chapter}章`,
      issue.line || "",
    ),
    h("p", { class: "issue-row__reason" }, issue.reason),
    issue.fallback ? h("p", { class: "issue-row__fallback" }, `降级：${issue.fallback}`) : null,
  );
}

async function build(route, host) {
  const booksPayload = await api.books();
  const books = booksPayload.books || [];
  let bookId = route.bookId;
  if (!bookId) bookId = books[0]?.id || null;
  const container = h("div");
  const refresh = () => render(host, { name: "issues", bookId });

  const picker = h(
    "select",
    {
      onChange: (event) => {
        window.location.hash = `#/book/${event.target.value}/issues`;
      },
    },
    ...books.map((book) => h("option", { value: book.id, selected: book.id === bookId }, book.title || book.id)),
  );
  container.append(
    h(
      "div",
      { class: "page-head" },
      h("div", { class: "page-head__title" }, h("h1", { class: "letterpress" }, "异常清单"), h("p", { class: "muted" }, "降级与失败都必须可见；批量重试会把相关章节重新排队。")),
      h(
        "div",
        { class: "page-head__actions" },
        books.length ? picker : null,
        h("a", { class: "btn btn-ghost", href: "#/jobs" }, icon("list-checks", { size: 14 }), "任务中心"),
      ),
    ),
  );
  if (!bookId) {
    container.append(emptyState("还没有书", "导入书稿之后，这里会列出分析/合成过程中的降级与失败。"));
    return container;
  }
  const { issues } = await api.issues(bookId);
  const { jobs } = await api.jobs().catch(() => ({ jobs: [] }));
  const failedJobs = (jobs || []).filter((job) => job.book_id === bookId && job.status === "failed");
  if (failedJobs.length) {
    container.append(
      h(
        "div",
        { class: "banner" },
        h(
          "span",
          {},
          `另有 ${failedJobs.length} 个失败任务（不属于降级记录）：`,
          failedJobs
            .slice(0, 6)
            .map((job) => `${job.kind}#${job.chapter_index ?? "全书"}`)
            .join("、"),
          failedJobs.length > 6 ? " …" : "",
        ),
        h("span", { class: "spacer" }),
        h("a", { class: "btn btn-sm", href: "#/jobs" }, icon("list-checks", { size: 12 }), "去任务中心重试"),
      ),
    );
  }
  if (!issues.length) {
    container.append(
      emptyState(
        "没有降级记录",
        failedJobs.length ? "但上面有失败任务，需要去任务中心处理。" : "全流程没有降级或失败记录。",
      ),
    );
    return container;
  }
  const counts = new Map();
  for (const issue of issues) counts.set(issue.kind, (counts.get(issue.kind) || 0) + 1);
  let filter = null;
  const list = h("div");
  const chips = h("div", { class: "chips" });
  const draw = () => {
    chips.replaceChildren(
      h(
        "button",
        {
          class: "chip",
          "aria-pressed": String(filter === null),
          onClick: () => {
            filter = null;
            draw();
          },
        },
        `全部 ${issues.length}`,
      ),
      ...[...counts.entries()].map(([kind, count]) =>
        h(
          "button",
          {
            class: "chip",
            "aria-pressed": String(filter === kind),
            onClick: () => {
              filter = kind;
              draw();
            },
          },
          `${issueKindLabel(kind)} ${count}`,
        ),
      ),
    );
    const visible = filter ? issues.filter((issue) => issue.kind === filter) : issues;
    list.replaceChildren(...visible.map(issueRow));
  };
  draw();
  container.append(
    h(
      "section",
      { class: "panel" },
      h(
        "div",
        { class: "panel__bar" },
        chips,
        h("span", { class: "spacer" }),
        h("span", { class: "count" }, `${shortId(bookId)} · 最近 ${issues.length} 条（倒序）`),
        h(
          "button",
          {
            class: "btn btn-sm",
            type: "button",
            onClick: async () => {
              try {
                const result = await api.retryIssues(bookId, filter ? [filter] : []);
                toast(result.chapters.length ? `已重新入队 ${result.chapters.length} 个章节` : "没有可重试的章节");
              } catch (error) {
                toast(error.message, "error");
              }
            },
          },
          icon("rotate-ccw", { size: 12 }),
          "批量重试",
        ),
      ),
      list,
    ),
  );
  return container;
}

export function render(host, route) {
  return renderWithState(host, () => build(route, host));
}
