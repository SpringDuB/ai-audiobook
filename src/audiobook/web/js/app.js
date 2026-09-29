import { store } from "./store.js";
import { parseHash, startRouter } from "./router.js";
import { errorState, h, runTeardowns } from "./ui.js";
import { icon } from "./icons.js";
import * as shelf from "./views/shelf.js";
import * as workspace from "./views/workspace.js";
import * as jobs from "./views/jobs.js";
import * as issues from "./views/issues.js";
import * as voices from "./views/voices.js";
import * as settings from "./views/settings.js";

// 书页与章节页都是同一个工作台，只是选中的章节不同
const VIEWS = { shelf, book: workspace, chapter: workspace, jobs, issues, voices, settings };

/* ------------------------------------------------------------------ 主题
   首访跟随系统（index.html 里的内联脚本负责在样式表之前落主题），
   用户点过之后存在 localStorage，之后都听用户的。 */

const THEME_BG = { light: "#f7f4ef", dark: "#12100e" };
const themeButton = document.getElementById("theme-toggle");

function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function paintTheme() {
  const theme = currentTheme();
  if (themeButton) {
    const next = theme === "dark" ? "浅色" : "深色";
    themeButton.replaceChildren(icon(theme === "dark" ? "moon" : "sun", { size: 17 }));
    themeButton.setAttribute("aria-label", `切换到${next}主题`);
    themeButton.title = `切换到${next}主题`;
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", THEME_BG[theme]);
}

themeButton?.addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try {
    localStorage.setItem("aiab-theme", next);
  } catch (error) {
    /* 隐身模式下写不了，刷新回落系统偏好即可 */
  }
  paintTheme();
});

paintTheme();

function crumbFor(route) {
  switch (route.name) {
    case "shelf":
      return "书架";
    case "book":
      return "书 / 章节";
    case "chapter":
      return `第 ${route.index} 章 · 校对台`;
    case "jobs":
      return "任务中心";
    case "issues":
      return "异常清单";
    case "voices":
      return "音色库";
    case "settings":
      return "设置";
    default:
      return "未找到";
  }
}

function paintStatus(state) {
  const dot = document.querySelector("#runstatus .dot");
  const label = document.getElementById("runlabel");
  if (!dot || !label) return;
  const running = state.jobs.filter((job) => job.status === "running");
  const queued = state.jobs.filter((job) => job.status === "queued");
  if (!state.connected && state.lastEventAt) {
    dot.dataset.state = "offline";
    label.textContent = "后台未连接";
    return;
  }
  if (running.length) {
    dot.dataset.state = "running";
    const job = running[0];
    const progress = job.progress || {};
    label.textContent = `${job.kind} ${progress.done ?? 0}/${progress.total ?? 0}`;
    return;
  }
  dot.dataset.state = "idle";
  label.textContent = queued.length ? `排队 ${queued.length}` : "空闲";
}

function paintRail(route) {
  const target = route.name === "book" || route.name === "chapter" ? "shelf" : route.name;
  for (const link of document.querySelectorAll("#rail a")) {
    const active = link.dataset.route === target || (target === "issues" && link.dataset.route === "issues");
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function notFound(route) {
  return errorState(`没有这个页面：${route.raw || ""}`, () => {
    window.location.hash = "#/shelf";
  });
}

const main = document.getElementById("main");
let token = 0;

function routeTo(route) {
  const mine = ++token;
  runTeardowns();
  document.getElementById("crumb").textContent = crumbFor(route);
  paintRail(route);
  main.dataset.view = route.name;
  const view = VIEWS[route.name];
  if (!view) {
    main.replaceChildren(notFound(route));
    return;
  }
  Promise.resolve(view.render(main, route)).then(() => {
    if (mine !== token) return;
    main.dataset.boot = "ready";
  });
}

store.subscribe(paintStatus);
store.startEvents();
startRouter(routeTo);

window.addEventListener("keydown", (event) => {
  if (event.target.closest("input, textarea, select")) return;
  if (event.key === "g") window.__goto = true;
  else if (window.__goto && event.key === "s") {
    window.location.hash = "#/shelf";
    window.__goto = false;
  } else window.__goto = false;
});

if (window.location.hash === "") window.location.hash = "#/shelf";
