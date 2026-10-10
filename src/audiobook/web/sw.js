/* AI 有声书 service worker

   两件事：
   1. 应用外壳缓存 —— 断网也能打开页面（导航回落到缓存的 "/"）。
   2. 章节离线包 —— 下载过的 /listen、/subtitles、/audio.m4a 走缓存；
      音频必须支持 Range（手机播放器靠它拖进度、iOS 靠它起播），
      缓存命中时自己切 206 分片，不要指望缓存里的整段 Response 能直接喂给播放器。

   缓存版本变了就在 activate 里清旧仓库；壳资源逐个 put，单个 404 不拖垮安装。 */

const SHELL_CACHE = "aiab-shell-v7";   // 改了壳资源（api.js/store.js/jobs.js/listen.js…）就升版本，装完自动清旧仓库
const OFFLINE_CACHE = "aiab-offline-v1";

const SHELL_ASSETS = [
  "/",
  "/manifest.webmanifest",
  "/static/theme.css",
  "/static/app.css",
  "/static/favicon.png",
  "/static/logo-mark.png",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
  "/static/icons/apple-touch-icon.png",
  "/static/js/app.js",
  "/static/js/api.js",
  "/static/js/format.js",
  "/static/js/icons.js",
  "/static/js/offline.js",
  "/static/js/router.js",
  "/static/js/store.js",
  "/static/js/ui.js",
  "/static/js/voicepicker.js",
  "/static/js/views/issues.js",
  "/static/js/views/jobs.js",
  "/static/js/views/listen.js",
  "/static/js/views/settings.js",
  "/static/js/views/shelf.js",
  "/static/js/views/voices.js",
  "/static/js/views/workspace.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    (async () => {
      const cache = await caches.open(SHELL_CACHE);
      await Promise.all(
        SHELL_ASSETS.map(async (url) => {
          try {
            const response = await fetch(url, { cache: "no-cache" });
            if (response.ok) await cache.put(url, response);
          } catch (error) {
            /* 单个资源失败不算安装失败，下次上线会补 */
          }
        }),
      );
      await self.skipWaiting();
    })(),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      const keys = await caches.keys();
      await Promise.all(
        keys.filter((key) => key !== SHELL_CACHE && key !== OFFLINE_CACHE).map((key) => caches.delete(key)),
      );
      await self.clients.claim();
    })(),
  );
});

function isOfflineAsset(pathname) {
  if (pathname.endsWith("/audio.m4a")) return true;
  if (pathname.endsWith("/subtitles")) return true;
  return /\/api\/books\/[^/]+\/listen$/.test(pathname);
}

async function matchOffline(request) {
  const cache = await caches.open(OFFLINE_CACHE);
  return (await cache.match(request)) || (await cache.match(request, { ignoreSearch: true }));
}

/* 把缓存里的整段音频切成 Range 分片；播放器拿到 206 才能拖进度。 */
async function withRange(request, response) {
  const range = request.headers.get("range");
  if (!range) return response;
  const match = /bytes=(\d*)-(\d*)/.exec(range);
  if (!match) return response;
  const blob = await response.blob();
  const size = blob.size;
  let start = match[1] ? Number(match[1]) : 0;
  let end = match[2] ? Number(match[2]) : size - 1;
  if (!Number.isFinite(start) || start < 0) start = 0;
  if (!Number.isFinite(end) || end >= size) end = size - 1;
  if (start > end || start >= size) {
    return new Response(null, { status: 416, headers: { "Content-Range": `bytes */${size}` } });
  }
  const headers = new Headers(response.headers);
  headers.set("Content-Range", `bytes ${start}-${end}/${size}`);
  headers.set("Content-Length", String(end - start + 1));
  headers.set("Accept-Ranges", "bytes");
  return new Response(blob.slice(start, end + 1), {
    status: 206,
    statusText: "Partial Content",
    headers,
  });
}

/* 下载过的离线资源：版本完全一致走缓存（省流量、断网也能放）；
   版本对不上（章节重渲染过）先联网拿新的，联网失败再退回旧的离线包。
   播放器请求 Range 时给 206。 */
async function offlineFirst(request) {
  const cache = await caches.open(OFFLINE_CACHE);
  const exact = await cache.match(request);
  if (exact) return withRange(request, exact);
  try {
    return await fetch(request);
  } catch (error) {
    const stale = await cache.match(request, { ignoreSearch: true });
    if (stale) return withRange(request, stale);
    throw error;
  }
}

/* 目录 / 字幕：联网时拿新的并顺手更新缓存，断网回落到缓存。 */
async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(OFFLINE_CACHE);
      await cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    const hit = await matchOffline(request);
    if (hit) return hit;
    throw error;
  }
}

async function staleWhileRevalidate(request, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(request, { ignoreSearch: true });
  const network = fetch(request)
    .then(async (response) => {
      if (response.ok) await cache.put(request, response.clone());
      return response;
    })
    .catch(() => null);
  return hit || (await network) || Response.error();
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (isOfflineAsset(url.pathname)) {
    // ?fresh=1：下载流程要拿服务器上的最新文件，绕开离线缓存
    if (url.searchParams.has("fresh")) {
      event.respondWith(fetch(request));
      return;
    }
    event.respondWith(url.pathname.endsWith("/audio.m4a") ? offlineFirst(request) : networkFirst(request));
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      (async () => {
        try {
          return await fetch(request);
        } catch (error) {
          const cache = await caches.open(SHELL_CACHE);
          const hit = (await cache.match("/")) || (await cache.match("/", { ignoreSearch: true }));
          if (hit) return hit;
          throw error;
        }
      })(),
    );
    return;
  }

  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    event.respondWith(staleWhileRevalidate(request, SHELL_CACHE));
  }
});
