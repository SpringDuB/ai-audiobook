// 任务中心：按状态分四栏（进行中 / 失败 / 已取消 / 已完成）。
// 页面上只有一个面板、列表在面板内部滚动，不再是四块叠成的一条长页。
// 数据两条腿：进页面拉一次快照，之后跟着 SSE 增量就地重画 —— 运行中的进度条
// 会自己走；页签记在模块里，切去别的页面再回来还停在上次那一栏。

import { api } from "../api.js";
import { clock, kindLabel, shortId } from "../format.js";
import { icon } from "../icons.js";
import { navigate } from "../router.js";
import { store } from "../store.js";
import { h, onTeardown, progressBar, renderWithState, toast } from "../ui.js";

const COLUMNS = ["任务", "对象", "进度", "状态", ""];

const TABS = [
  {
    id: "active",
    label: "进行中",
    icon: "hourglass",
    statuses: ["running", "queued"],
    empty: ["没有进行中的任务", "worker 正在待命：去书页点「生成」「分析」就会派活。"],
  },
  {
    id: "failed",
    label: "失败",
    icon: "triangle-alert",
    statuses: ["failed"],
    tone: "danger",
    empty: ["没有失败的任务", "跑挂的任务会落到这里，可以逐条重试。"],
  },
  {
    id: "canceled",
    label: "已取消",
    icon: "x",
    statuses: ["canceled"],
    empty: ["没有取消的任务", "中途停下的任务会留在这里，也能重新排队。"],
  },
  {
    id: "done",
    label: "已完成",
    icon: "circle-check",
    statuses: ["done"],
    empty: ["还没有完成的任务", "跑完的任务按时间倒序排在这里。"],
  },
];

// 切去别的页面再回来，还停在上次看的页签
let activeTab = "active";

function statusDot(job) {
  const state = job.status === "running" && job.cancel_requested ? "canceled" : job.status;
  return h("span", { class: "dot", dataset: { state }, "aria-hidden": "true" });
}

function statusTag(job) {
  if (job.status === "running" && job.cancel_requested) {
    return h("span", { class: "tag tag--danger" }, `取消中… ${job.attempts}/${job.max_attempts}`);
  }
  if (job.status === "running") return h("span", { class: "tag tag--accent" }, `运行中 ${job.attempts}/${job.max_attempts}`);
  if (job.status === "failed") return h("span", { class: "tag tag--danger" }, `失败 ${job.attempts}/${job.max_attempts}`);
  if (job.status === "canceled") return h("span", { class: "tag" }, `已取消 ${job.attempts}/${job.max_attempts}`);
  if (job.status === "queued") return h("span", { class: "tag" }, `排队 ${job.attempts}/${job.max_attempts}`);
  return h("span", { class: "tag tag--ok" }, `完成 ${job.attempts}/${job.max_attempts}`);
}

function ago(ms) {
  if (!ms) return "—";
  const diff = Date.now() - Number(ms);
  if (diff < 45_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.max(1, Math.round(diff / 60_000))} 分钟前`;
  if (diff < 86_400_000) return `${Math.round(diff / 3_600_000)} 小时前`;
  return clock(ms);
}

function stamp() {
  return new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

function targetHash(job) {
  if (!job.book_id) return null;
  if (job.chapter_index === null || job.chapter_index === undefined) return `#/book/${job.book_id}`;
  return `#/book/${job.book_id}/chapter/${job.chapter_index}`;
}

function jobRow(job, refresh, titles) {
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
          title: "重新排队：跑过的部分走缓存，不用从头再来",
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
    if (progress.message) {
      state.append(h("span", { class: "job-row__meta", title: progress.message }, progress.message));
    }
  } else {
    const message = progress?.message || job.error || "—";
    state.append(
      h("span", { class: job.status === "failed" ? "job-row__msg" : "job-row__meta", title: message }, message),
    );
  }

  const where = job.chapter_index === null || job.chapter_index === undefined ? "全书" : `第 ${job.chapter_index} 章`;
  const rawTitle = titles.get(job.book_id) || shortId(job.book_id);
  const title = rawTitle.length > 14 ? `${rawTitle.slice(0, 14)}…` : rawTitle;
  const meta = `${where} · ${title}`;
  const hash = targetHash(job);
  const target = hash
    ? h("button", { class: "job-row__target", type: "button", title: `去书页看：${meta}`, onClick: () => navigate(hash) }, meta)
    : h("span", { class: "job-row__meta" }, meta);

  return h(
    "div",
    { class: "job-row", dataset: { status: job.status } },
    h("span", { class: "job-row__id", title: `任务 #${job.id}` }, statusDot(job), `#${job.id}`),
    h("span", { class: "job-row__what" }, h("span", { class: "job-row__kind" }, kindLabel(job.kind)), target),
    state,
    h(
      "span",
      { class: "job-row__status" },
      statusTag(job),
      h("span", { class: "job-row__meta", title: clock(job.updated_at) }, ago(job.updated_at)),
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

function tabButton(tab, count, onPick) {
  return h(
    "button",
    {
      class: tab.tone === "danger" ? "tab tab--danger" : "tab",
      type: "button",
      role: "tab",
      "aria-pressed": String(tab.id === activeTab),
      "aria-selected": String(tab.id === activeTab),
      onClick: onPick,
    },
    icon(tab.icon, { size: 13 }),
    tab.label,
    h("span", { class: "tab__count" }, String(count)),
  );
}

function sortedFor(tab, jobs) {
  const rows = jobs.filter((job) => tab.statuses.includes(job.status));
  if (tab.id === "active") {
    // 跑着的在前（先开的先排），排队的按入队顺序
    return rows.sort((a, b) => (a.status === b.status ? a.id - b.id : a.status === "running" ? -1 : 1));
  }
  return rows.sort((a, b) => b.id - a.id); // 失败 / 取消 / 完成：新的在前
}

async function build(host) {
  // 快照：活跃单独取一次，免得排很久的任务被历史挤出窗口；其余按状态各取一档
  const snapshot = new Map();
  const titles = new Map();
  const fetchSnapshot = async () => {
    const [active, failed, canceled, recent] = await Promise.all([
      api.jobs({ status: "running,queued", limit: 300 }),
      api.jobs({ status: "failed", limit: 200 }),
      api.jobs({ status: "canceled", limit: 200 }),
      api.jobs({ limit: 200 }),
    ]);
    for (const payload of [recent, failed, canceled, active]) {
      for (const job of payload.jobs || []) snapshot.set(job.id, job);
    }
  };
  await fetchSnapshot();
  await api
    .books()
    .then((payload) => {
      for (const book of payload.books || []) titles.set(book.id, book.title);
    })
    .catch(() => {}); // 拿不到书名单不影响看任务，退回短 id

  const allJobs = () => {
    // SSE 增量优先：后到的状态覆盖快照
    for (const job of store.state.jobs) snapshot.set(job.id, job);
    return [...snapshot.values()];
  };

  const tabBar = h("div", { class: "tabs", role: "tablist" });
  const live = h("span", { class: "jobs__live" });
  const body = h("div", { class: "jobs__scroll" });

  const paint = () => {
    const jobs = allJobs();
    tabBar.replaceChildren(
      ...TABS.map((tab) =>
        tabButton(
          tab,
          jobs.filter((job) => tab.statuses.includes(job.status)).length,
          () => {
            activeTab = tab.id;
            paint();
          },
        ),
      ),
    );

    const tab = TABS.find((item) => item.id === activeTab) || TABS[0];
    const rows = sortedFor(tab, jobs);
    body.replaceChildren(
      ...(rows.length
        ? [head(), ...rows.map((job) => jobRow(job, refresh, titles))]
        : [
            h(
              "div",
              { class: "jobs__empty" },
              icon(tab.icon, { size: 22 }),
              h("p", {}, tab.empty[0]),
              h("p", { class: "muted" }, tab.empty[1]),
            ),
          ]),
    );

    const running = jobs.filter((job) => job.status === "running").length;
    const queued = jobs.filter((job) => job.status === "queued").length;
    live.replaceChildren(
      h("span", { class: "dot", dataset: { state: running ? "running" : "idle" }, "aria-hidden": "true" }),
      h(
        "span",
        {},
        running
          ? `${running} 个任务运行中${queued ? ` · ${queued} 个排队` : ""}`
          : queued
            ? `${queued} 个排队等 worker`
            : "worker 空闲",
      ),
      h("span", { class: "jobs__stamp" }, stamp()),
    );
  };

  // 手动刷新：重拉快照就地重画（不整页重建，免得闪一下、丢滚动位置）
  const refresh = async () => {
    try {
      await fetchSnapshot();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      paint();
    }
  };

  let timer = null;
  const unsubscribe = store.subscribe(() => {
    if (timer) return; // SSE 每帧都重画没必要：合并到 350ms 一次
    timer = window.setTimeout(() => {
      timer = null;
      paint();
    }, 350);
  });
  onTeardown(() => {
    unsubscribe();
    if (timer) window.clearTimeout(timer);
  });

  paint();
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
    h("div", { class: "jobs__toolbar" }, tabBar, live),
    h("section", { class: "panel jobs__panel" }, body),
  );
}

export function render(host) {
  return renderWithState(host, () => build(host));
}
