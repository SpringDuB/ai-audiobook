import { icon } from "./icons.js";

export function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style") Object.assign(node.style, value);
    // "onClick" → "click"（DOM 事件名大小写敏感，必须转小写）
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === "html") node.innerHTML = value;
    else if (key in node && key !== "list") node[key] = value;
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function seal(name) {
  const label = String(name || "?").trim();
  const initial = label === "旁白" ? "白" : label.slice(0, 1);
  return h("span", { class: label === "旁白" ? "seal seal--muted" : "seal", title: label, "aria-label": label }, initial);
}

export function loadingState(label = "装版中") {
  return h("div", { class: "loading" }, label);
}

export function emptyState(title, hint, action = null) {
  return h(
    "div",
    { class: "empty" },
    h("p", {}, title),
    hint ? h("p", { class: "muted" }, hint) : null,
    action || null,
  );
}

export function errorState(message, retry = null) {
  return h(
    "div",
    { class: "error-block" },
    h("p", {}, `出错了：${message}`),
    retry ? h("button", { class: "btn btn-sm", onClick: retry }, "重试") : null,
  );
}

export function renderWithState(host, loader) {
  const run = async () => {
    host.replaceChildren(loadingState());
    try {
      const nodes = await loader();
      host.replaceChildren(...(Array.isArray(nodes) ? nodes : [nodes]));
    } catch (error) {
      host.replaceChildren(errorState(error?.message || String(error), run));
    }
  };
  return run();
}

// 视图切换时清理（定时器、事件监听、共享 audio 元素都挂在这里）
const teardowns = new Set();

export function onTeardown(fn) {
  teardowns.add(fn);
  return () => teardowns.delete(fn);
}

export function runTeardowns() {
  for (const fn of teardowns) {
    try {
      fn();
    } catch {
      /* 清理失败不该拦住页面切换 */
    }
  }
  teardowns.clear();
}

let toastTimer = null;

export function toast(message, kind = "info") {
  const node = document.getElementById("toast");
  if (!node) return;
  node.textContent = message;
  node.dataset.kind = kind;
  node.classList.add("is-open");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("is-open"), kind === "error" ? 5200 : 2600);
}

// 破坏性操作的二次确认：返回 Promise<boolean>，Esc / 点遮罩算取消
export function confirmDialog({ title, message, confirmLabel = "确定", cancelLabel = "取消", danger = false }) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      document.removeEventListener("keydown", onKey, true);
      host.remove();
      resolve(value);
    };
    const onKey = (event) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        finish(false);
      } else if (event.key === "Enter") {
        event.stopPropagation();
        finish(true);
      }
    };
    const confirm = h(
      "button",
      { class: `btn ${danger ? "btn-danger" : "btn-primary"}`, type: "button", onClick: () => finish(true) },
      icon(danger ? "trash-2" : "check", { size: 13 }),
      confirmLabel,
    );
    const panel = h(
      "div",
      { class: "modal", role: "dialog", "aria-modal": "true", "aria-label": title },
      h("h2", { class: "modal__title letterpress" }, title),
      message ? h("p", { class: "modal__message" }, message) : null,
      h(
        "div",
        { class: "modal__actions" },
        h("button", { class: "btn btn-ghost", type: "button", onClick: () => finish(false) }, cancelLabel),
        confirm,
      ),
    );
    const host = h(
      "div",
      { class: "modal-host" },
      h("div", { class: "modal-host__backdrop", onClick: () => finish(false) }),
      panel,
    );
    document.body.append(host);
    document.addEventListener("keydown", onKey, true);
    confirm.focus();
  });
}

// 章节多选：给「分析角色文本」这类按章跑的任务用；返回勾选的章节序号数组（取消为 null）
export function chapterPickerDialog({
  title,
  message,
  chapters,
  confirmLabel = "开始",
  selected = null,
  rerunLabel = "",
}) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      document.removeEventListener("keydown", onKey, true);
      host.remove();
      resolve(value);
    };
    const onKey = (event) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        finish(null);
      }
    };
    const wanted = new Set(selected ?? chapters.map((chapter) => chapter.index));
    const boxes = new Map();
    const hint = h("span", { class: "mono muted" });
    const rows = chapters.map((chapter) => {
      const box = h("input", { class: "pick-row__box", type: "checkbox", checked: wanted.has(chapter.index) });
      boxes.set(chapter.index, box);
      return h(
        "label",
        { class: "pick-row" },
        box,
        h("span", { class: "pick-row__no mono" }, String(chapter.index)),
        h("span", { class: "pick-row__title" }, chapter.title || `第${chapter.index}章`),
        h(
          "span",
          { class: "pick-row__meta mono" },
          `${chapter.lines || 0} 句${chapter.duration_sec ? " · 已出音频" : ""}`,
        ),
      );
    });
    const pick = (value) => {
      boxes.forEach((box) => {
        box.checked = value;
      });
      hint.textContent = value ? `已选 ${boxes.size} 章` : "未选章节";
    };
    const confirm = h(
      "button",
      {
        class: "btn btn-primary",
        type: "button",
        onClick: () => {
          const picked = [...boxes.entries()].filter(([, box]) => box.checked).map(([index]) => index);
          if (!picked.length) {
            hint.textContent = "至少勾选一章";
            return;
          }
          finish({ picked, force: Boolean(rerun && rerun.checked) });
        },
      },
      confirmLabel,
    );
    // 「覆盖重跑」开关：默认关闭 → 已经分析好的章会被跳过（断点续跑）
    const rerun = rerunLabel
      ? h("input", { class: "pick-row__box", type: "checkbox" })
      : null;
    const panel = h(
      "div",
      { class: "modal modal--wide", role: "dialog", "aria-modal": "true", "aria-label": title },
      h("h2", { class: "modal__title letterpress" }, title),
      message ? h("p", { class: "modal__message" }, message) : null,
      h(
        "div",
        { class: "modal__tools" },
        h("button", { class: "btn btn-sm", type: "button", onClick: () => pick(true) }, "全选"),
        h("button", { class: "btn btn-sm", type: "button", onClick: () => pick(false) }, "全不选"),
        hint,
      ),
      h("div", { class: "modal__list" }, ...rows),
      rerun
        ? h("label", { class: "switch-row" }, rerun, h("span", { class: "switch-row__label" }, rerunLabel))
        : null,
      h(
        "div",
        { class: "modal__actions" },
        h("button", { class: "btn btn-ghost", type: "button", onClick: () => finish(null) }, "取消"),
        confirm,
      ),
    );
    const host = h(
      "div",
      { class: "modal-host" },
      h("div", { class: "modal-host__backdrop", onClick: () => finish(null) }),
      panel,
    );
    hint.textContent = `已选 ${[...boxes.values()].filter((box) => box.checked).length} 章`;
    document.body.append(host);
    document.addEventListener("keydown", onKey, true);
    confirm.focus();
  });
}

export function progressBar(done, total) {
  const value = total ? Math.round((done / total) * 100) : 0;
  return h(
    "span",
    { class: "progress" },
    h("span", { class: "progress__bar" }, h("span", { style: { width: `${value}%` } })),
    h("span", { class: "mono" }, `${done}/${total}`),
  );
}

export function tag(text, variant = "") {
  return h("span", { class: variant ? `tag ${variant}` : "tag" }, text);
}
