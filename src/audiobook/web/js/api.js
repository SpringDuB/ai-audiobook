export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function readError(response) {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
    if (Array.isArray(body?.detail)) return body.detail.map((item) => item.msg || JSON.stringify(item)).join("；");
    return JSON.stringify(body);
  } catch {
    return `${response.status} ${response.statusText}`;
  }
}

async function request(method, url, body, options = {}) {
  const init = { method, headers: {}, ...options };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(url, init);
  if (!response.ok) throw new ApiError(await readError(response), response.status);
  if (response.status === 204) return null;
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response.text();
}

const get = (url) => request("GET", url);
const send = (method, url, body) => request(method, url, body ?? {});

export const api = {
  books: () => get("/api/books"),
  book: (id) => get(`/api/books/${id}`),
  chapters: (id) => get(`/api/books/${id}/chapters`),
  chapterText: (id, index) => get(`/api/books/${id}/chapters/${index}/text`),
  lines: (id, index, scene) =>
    get(`/api/books/${id}/chapters/${index}/lines`),
  patchLine: (id, lineId, patch) => send("PATCH", `/api/books/${id}/lines/${lineId}`, patch),
  resynth: (id, lineId) => send("POST", `/api/books/${id}/lines/${lineId}/resynth`),
  renderChapter: (id, index) => send("POST", `/api/books/${id}/chapters/${index}/render`),
  runBook: (id) => send("POST", `/api/books/${id}/run`),
  analyzeBook: (id) => send("POST", `/api/books/${id}/analyze`),
  analyzeChapter: (id, index) => send("POST", `/api/books/${id}/chapters/${index}/analyze`),
  generateBook: (id) => send("POST", `/api/books/${id}/generate`),
  exportBook: (id, body) => send("POST", `/api/books/${id}/export`, body),
  deleteBook: (id) => request("DELETE", `/api/books/${id}`),
  upload: (file, title) => {
    const form = new FormData();
    form.append("file", file);
    form.append("title", title);
    return request("POST", "/api/books", form);
  },
  jobs: () => get("/api/jobs"),
  cancelJob: (id) => send("POST", `/api/jobs/${id}/cancel`),
  retryJob: (id) => send("POST", `/api/jobs/${id}/retry`),
  issues: (id) => get(`/api/books/${id}/issues`),
  retryIssues: (id, kinds) => send("POST", `/api/books/${id}/issues/retry`, { kinds }),
  voices: () => get("/api/voices"),
  casting: (id) => get(`/api/books/${id}/casting`),
  setCasting: (id, roleId, body) => send("PUT", `/api/books/${id}/casting/${roleId}`, body),
  settings: () => get("/api/settings"),
  saveSettings: (patch) => send("PUT", "/api/settings", patch),
  ttsLocal: () => get("/api/tts/local"),
  ttsStart: (body) => send("POST", "/api/tts/local/start", body || {}),
  ttsStop: () => send("POST", "/api/tts/local/stop"),
  ttsLogs: (offset) => get(`/api/tts/local/logs?offset=${Number(offset) || 0}`),
};
