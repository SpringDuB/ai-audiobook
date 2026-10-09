// 任务中心：运行中 / 排队中 / 失败 / 最近完成。
// 列表带列头，进度与错误信息各占一列，长错误不会把别的字段挤变形。

import { api } from "../api.js";
import { clock, jobStateLabel, kindLabel, shortId } from "../format.js";
import { icon } from "../icons.js";
import { h, progressBar, renderWithState, toast } from "../ui.js";

const COLUMNS = ["任务", "对象", "进度", "状态", ""];

function statusTag(job) {
  if (job.status === "running" && job.cancel_requested) {
    return h("span", { class: "tag tag--danger" }, `取消中… ${job.attempts}/${job.max_attempts}`);
  }
  if (job.status === "running") return h("span", { class: "tag tag--accent" }, `运行中 ${job.attempts}/${job.max_attempts}`);
  if (job.status === "failed") return h("span", { class: "tag tag--danger" }, `失败 ${job.attempts}/${job.max_attempts}`);
  if (job.status === "canceled") return h("span", { class: "tag" }, `已取消 ${job.attempts}/${job.max_attempts}`);
  if (job.status === "queued") return h("span", { class: "tag" }, `排队 ${job.attempts}/${job.max_attempts}`);
  return h("span", { class: "tag" }, `完成 ${job.attempts}/${job.max_attempts}`);
}

function jobRow(job, refresh) {
  const progress = job.progress || null;
  const actions = [];
  if (job.status === "running" && job.cancel_requested) {
    // 取消请求已发出：worker 会在几秒内停下来（LLM 流会在数据块之间被打断）
    actions.push(
      h(
        "button",
        { class: "btn btn-sm", type: "button", disabled: true, title: "已请求取消，等 worker 停下来" },
        icon("hourglass", { size: 12 }),
        "取消中…",
      ),
    );
  } else if (job.status === "running" || job.status === "queued") {
    actions.push(
      h(
        "button",
        {
          class: "btn btn-sm btn-danger",
          type: "button",
          title: "立刻停下这个任务（正在跑的模型请求会被打断）",
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
        icon("x", { size: 12 }),
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
          type: "button",
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
        icon("rotate-ccw", { size: 12 }),
        "重试",
      ),
    );
  }

  const state = h("span", { class: "job-row__state" });
  if (job.status === "running" && progress) {
    state.append(progressBar(progress.done || 0, progress.total || 0));
    if (progress.message) state.append(h("span", { class: "job-row__meta" }, progress.message));
  } else {
    state.append(
      h(
        "span",
        { class: job.status === "failed" ? "job-row__msg" : "job-row__meta" },
        progress?.message || job.error || "—",
      ),
    );
  }

  return h(
    "div",
    { class: "job-row", dataset: { status: job.status } },
    h("span", { class: "job-row__id" }, `#${job.id}`),
    h(
      "span",
      { class: "job-row__what" },
      h("span", { class: "job-row__kind" }, kindLabel(job.kind)),
      h(
        "span",
        { class: "job-row__meta" },
        shortId(job.book_id),
        job.chapter_index === null || job.chapter_index === undefined ? " · 全书" : ` · 第${job.chapter_index}章`,
      ),
    ),
    state,
    h(
      "span",
      { class: "job-row__status" },
      statusTag(job),
      h("span", { class: "job-row__meta" }, clock(job.updated_at)),
    ),
    h("span", { class: "job-row__act" }, ...actions),
  );
}

function head() {
  return h(
    "div",
    { class: "list-head", "aria-hidden": "true" },
    ...COLUMNS.map((label) => h("span", {}, label)),
  );
}

function block(title, jobs, hint, refresh) {
  const rows = jobs.length
    ? [head(), ...jobs.map((job) => jobRow(job, refresh))]
    : h("p", { class: "panel__body muted" }, hint);
  return h(
    "section",
    { class: "panel" },
    h(
      "div",
      { class: "panel__bar" },
      h("h2", {}, title),
      h("span", { class: "count" }, String(jobs.length)),
    ),
    rows,
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
      h(
        "div",
        { class: "page-head__title" },
        h("h1", { class: "letterpress" }, "任务中心"),
        h("p", { class: "muted" }, "长任务都在 worker 里跑；关掉浏览器不会中断。"),
      ),
      h(
        "div",
        { class: "page-head__actions" },
        h("button", { class: "btn", type: "button", onClick: refresh }, icon("refresh-cw", { size: 14 }), "刷新"),
      ),
    ),
    h(
      "div",
      { class: "stat-row" },
      h("div", { class: "stat", dataset: { tone: "accent" } }, h("div", { class: "stat__num" }, String(running.length)), h("div", { class: "stat__label" }, "运行中")),
      h("div", { class: "stat" }, h("div", { class: "stat__num" }, String(queued.length)), h("div", { class: "stat__label" }, "排队中")),
      h(
        "div",
        { class: "stat", dataset: failed.length ? { tone: "danger" } : {} },
        h("div", { class: "stat__num" }, String(failed.length)),
        h("div", { class: "stat__label" }, "失败"),
      ),
      h("div", { class: "stat" }, h("div", { class: "stat__num" }, String(done.length)), h("div", { class: "stat__label" }, "最近完成")),
    ),
    block("运行中", running, "当前没有运行中的任务。", refresh),
    block("排队中", queued, "队列是空的。", refresh),
    block("失败", failed, "没有失败任务。", refresh),
    block("最近完成", doneToday, "还没有完成的任务。", refresh),
  );
}

export function render(host) {
  return renderWithState(host, () => build(host));
}
