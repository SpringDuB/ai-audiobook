export function parseHash(hash) {
  const parts = String(hash || "")
    .replace(/^#\/?/, "")
    .split("/")
    .filter(Boolean)
    .map(decodeURIComponent);
  if (parts.length === 0) return { name: "shelf" };
  const [head, ...rest] = parts;
  if (["shelf", "jobs", "voices", "settings"].includes(head)) return { name: head };
  if (head === "issues") return { name: "issues", bookId: rest[0] || null };
  if (head === "book" && rest[0]) {
    if (rest[1] === "chapter" && rest[2] !== undefined) {
      return { name: "chapter", bookId: rest[0], index: Number(rest[2]) };
    }
    if (rest[1] === "issues") return { name: "issues", bookId: rest[0] };
    return { name: "book", bookId: rest[0] };
  }
  return { name: "notfound", raw: hash };
}

export function startRouter(onRoute) {
  const handler = () => onRoute(parseHash(window.location.hash));
  window.addEventListener("hashchange", handler);
  handler();
}

export function navigate(hash) {
  if (window.location.hash === hash) {
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    return;
  }
  window.location.hash = hash;
}
