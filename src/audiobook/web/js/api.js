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
  // 书目信息：默认会带上整本书的正文。界面只用到书名/产出清单，所以传 { chapters: "none" }，
  // 否则打开一本书要多传 2~8MB 的 JSON（原文另有 chapterText 接口按章取）。
  book: (id, options) => get(`/api/books/${id}${options?.chapters === "none" ? "?chapters=none" : ""}`),
  chapters: (id) => get(`/api/books/${id}/chapters`),
  chapterText: (id, index) => get(`/api/books/${id}/chapters/${index}/text`),
  lines: (id, index, scene) =>
    get(`/api/books/${id}/chapters/${index}/lines`),
  patchLine: (id, lineId, patch) => send("PATCH", `/api/books/${id}/lines/${lineId}`, patch),
  resynth: (id, lineId) => send("POST", `/api/books/${id}/lines/${lineId}/resynth`),
  renderChapter: (id, index) => send("POST", `/api/books/${id}/chapters/${index}/render`),
  runBook: (id) => send("POST", `/api/books/${id}/run`),
  analyzeBook: (id, force = false) => send("POST", `/api/books/${id}/analyze?force=${force ? "true" : "false"}`),
  analyzeChapter: (id, index) => send("POST", `/api/books/${id}/chapters/${index}/analyze`),
  analyzeChapters: (id, chapters, force = false) =>
    send("POST", `/api/books/${id}/analyze/chapters`, { chapters, force }),
  generateBook: (id) => send("POST", `/api/books/${id}/generate`),
  generateChapter: (id, index) => send("POST", `/api/books/${id}/chapters/${index}/generate`),
  exportBook: (id, body) => send("POST", `/api/books/${id}/export`, body),
  output: (id) => get(`/api/books/${id}/output`),
  revealOutput: (id) => send("POST", `/api/books/${id}/output/reveal`),
  listenCatalog: (id) => get(`/api/books/${id}/listen`),
  subtitles: (id, index) => get(`/api/books/${id}/chapters/${index}/subtitles`),
  prepareMobile: (id, index, body) => send("POST", `/api/books/${id}/chapters/${index}/mobile`, body ?? {}),
  deleteBook: (id) => request("DELETE", `/api/books/${id}`),
  upload: (file, title) => {
    const form = new FormData();
    form.append("file", file);
    form.append("title", title);
    return request("POST", "/api/books", form);
  },
  // 任务列表：默认全量（老调用照旧）；传 { status: "running,queued", limit: 200 } 只取需要的
  jobs: (options = {}) => {
    const params = new URLSearchParams();
    if (options.status) params.set("status", options.status);
    if (options.limit) params.set("limit", String(options.limit));
    const query = params.toString();
    return get(`/api/jobs${query ? `?${query}` : ""}`);
  },
  cancelJob: (id) => send("POST", `/api/jobs/${id}/cancel`),
  retryJob: (id) => send("POST", `/api/jobs/${id}/retry`),
  issues: (id) => get(`/api/books/${id}/issues`),
  retryIssues: (id, kinds) => send("POST", `/api/books/${id}/issues/retry`, { kinds }),
  voices: ({ includeDisabled = false } = {}) => get(`/api/voices${includeDisabled ? "?include_disabled=true" : ""}`),
  uploadVoice: (form) => request("POST", "/api/voices", form),
  patchVoice: (id, patch) => send("PATCH", `/api/voices/${id}`, patch),
  casting: (id) => get(`/api/books/${id}/casting`),
  setCasting: (id, roleId, body) => send("PUT", `/api/books/${id}/casting/${roleId}`, body),
  saveRoleDescription: (id, roleId, body) => send("PUT", `/api/books/${id}/roles/${roleId}/description`, body),
  rewriteRoleDescription: (id, roleId, body) => send("POST", `/api/books/${id}/roles/${roleId}/rewrite`, body),
  previewRole: (id, roleId, body) => send("POST", `/api/books/${id}/roles/${roleId}/preview`, body),
  previewAllRoles: (id, body) => send("POST", `/api/books/${id}/roles/preview_all`, body),
  job: (jobId) => get(`/api/jobs/${jobId}`),
  rolePreviewUrl: (id, roleId, stamp) =>
    `/api/books/${id}/roles/${roleId}/preview.wav${stamp ? `?v=${stamp}` : ""}`,
  settings: () => get("/api/settings"),
  saveSettings: (patch) => send("PUT", "/api/settings", patch),
  ttsLocal: () => get("/api/tts/local"),
  ttsStart: (body) => send("POST", "/api/tts/local/start", body || {}),
  ttsStop: () => send("POST", "/api/tts/local/stop"),
  ttsLogs: (offset) => get(`/api/tts/local/logs?offset=${Number(offset) || 0}`),
};
