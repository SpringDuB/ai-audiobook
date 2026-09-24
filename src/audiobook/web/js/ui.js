export function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "style") Object.assign(node.style, value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
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
