import { api } from "../api.js";
import { clock, jobStateLabel, kindLabel, percent, shortId } from "../format.js";
import { emptyState, h, progressBar, renderWithState, toast } from "../ui.js";

function jobRow(job, refresh) {
  const progress = job.progress || null;
  const actions = [];
  if (job.status === "running" || job.status === "queued") {
    actions.push(
      h(
        "button",
        {
          class: "btn btn-sm btn-danger",
          onClick: async () => {
            try {
              await api.cancelJob(job.id);
              toast(`已请求取消 #${job.id}`);
            } catch (error) {
              toast(error.message, "error");
            } finally {
              refresh();
            }
          },
        },
        "取消",
      ),
    );
  }
  if (job.status === "failed" || job.status === "canceled") {
    actions.push(
      h(
        "button",
        {
          class: "btn btn-sm",
          onClick: async () => {
            try {
              await api.retryJob(job.id);
              toast(`#${job.id} 已重新排队`);
            } catch (error) {
              toast(error.message, "error");
            } finally {
              refresh();
            }
          },
        },
        "重试",
      ),
    );
  }
  return h(
    "div",
    { class: "job-row" },
    h("span", { class: "mono" }, `#${job.id}`),
    h(
      "span",
      {},
      h("div", { class: "job-row__kind" }, kindLabel(job.kind)),
      h("div", { class: "job-row__meta mono" }, shortId(job.book_id), job.chapter_index === null ? "" : ` · 第${job.chapter_index}章`),
    ),
    h(
      "span",
      {},
      job.status === "running" && progress
        ? progressBar(progress.done || 0, progress.total || 0)
        : h("span", { class: "job-row__meta" }, progress?.message || job.error || "—"),
      job.status === "running" && progress?.message
        ? h("div", { class: "job-row__meta" }, progress.message)
        : null,
    ),
    h(
      "span",
      { class: "row" },
      h("span", { class: "job-row__meta mono" }, `${jobStateLabel(job.status)} ${job.attempts}/${job.max_attempts}`),
      h("span", { class: "job-row__meta mono" }, clock(job.updated_at)),
      ...actions,
    ),
  );
}

function block(title, jobs, hint, refresh) {
  return h(
    "section",
    { class: "job-block" },
    h("div", { class: "row row--between" }, h("h2", { class: "letterpress" }, title), h("span", { class: "muted mono" }, `${jobs.length}`)),
    jobs.length
      ? h("div", { class: "sheet" }, ...jobs.map((job) => jobRow(job, refresh)))
      : h("p", { class: "muted" }, hint),
  );
}

async function build(host) {
  const { jobs } = await api.jobs();
  const refresh = () => render(host, { name: "jobs" });
  const running = jobs.filter((job) => job.status === "running");
  const queued = jobs.filter((job) => job.status === "queued");
  const failed = jobs.filter((job) => job.status === "failed");
  const done = jobs.filter((job) => job.status === "done");
  const doneToday = done.slice(-8).reverse();
  return h(
    "div",
    {},
    h(
      "div",
      { class: "page-head" },
      h("div", { class: "page-head__title" }, h("h1", { class: "letterpress" }, "任务中心"), h("p", { class: "muted" }, "长任务都在 worker 里跑；关掉浏览器不会中断。")),
      h("div", { class: "page-head__actions" }, h("button", { class: "btn", onClick: refresh }, "刷新")),
    ),
    block("运行中", running, "当前没有运行中的任务。", refresh),
    block("排队中", queued, "队列是空的。", refresh),
    block("失败", failed, "没有失败任务。", refresh),
    h("section", { class: "job-block" }, h("h2", { class: "letterpress" }, "最近完成"), doneToday.length ? h("div", { class: "sheet" }, ...doneToday.map((job) => jobRow(job, refresh))) : h("p", { class: "muted" }, "还没有完成的任务。")),
  );
}

export function render(host) {
  return renderWithState(host, () => build(host));
}
