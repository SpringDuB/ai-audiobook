// 离线听书：service worker 注册 + 章节离线包（缓存到 Cache Storage）。
//
// 缓存键用不带版本号的稳定地址（/audio.m4a），播放时带 ?v=mtime 也能命中；
// 版本记在 localStorage 里，章节重渲染后能对比出「离线包是旧的」。
// 注意：service worker 只在安全上下文可用 —— 手机经局域网 http://192.168.x.x
// 访问时没有 SW，只能在线听；要离线得走 HTTPS（Tailscale / 隧道 / 本机 localhost）。

import { api } from "./api.js";

export const OFFLINE_CACHE = "aiab-offline-v1";
const META_KEY = "aiab-offline-meta";

export function offlineSupported() {
  return Boolean(window.isSecureContext && "serviceWorker" in navigator && "caches" in window);
}

export async function registerServiceWorker() {
  if (!offlineSupported()) return null;
  try {
    return await navigator.serviceWorker.register("/sw.js", { scope: "/" });
  } catch (error) {
    console.warn("service worker 注册失败", error);
    return null;
  }
}

function readMeta() {
  try {
    const raw = JSON.parse(localStorage.getItem(META_KEY) || "{}");
    return raw && typeof raw === "object" ? raw : {};
  } catch (error) {
    return {};
  }
}

function writeMeta(meta) {
  try {
    localStorage.setItem(META_KEY, JSON.stringify(meta));
  } catch (error) {
    /* 隐私模式写不了：这次下载依然可用，只是"已下载"标记不持久 */
  }
}

const keyOf = (bookId, index) => `${bookId}:${index}`;

export function downloadInfo(bookId, index) {
  return readMeta()[keyOf(bookId, index)] || null;
}

export function downloadIsCurrent(bookId, chapter) {
  const info = downloadInfo(bookId, chapter.index);
  if (!info) return false;
  if (!chapter.m4a_mtime || !info.mtime) return Boolean(info.mtime);
  return Math.abs(Number(chapter.m4a_mtime) - Number(info.mtime)) < 0.001;
}

export async function requestPersistentStorage() {
  try {
    if (navigator.storage?.persist) return await navigator.storage.persist();
  } catch (error) {
    /* 不支持就算了 */
  }
  return false;
}

export async function storageEstimate() {
  try {
    if (navigator.storage?.estimate) return await navigator.storage.estimate();
  } catch (error) {
    /* 同上 */
  }
  return null;
}

async function readWithProgress(response, total, onProgress) {
  if (!response.body?.getReader) return response.blob();
  const reader = response.body.getReader();
  const chunks = [];
  let loaded = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    loaded += value.length;
    onProgress?.(loaded, total || loaded);
  }
  return new Blob(chunks, { type: response.headers.get("content-type") || "audio/mp4" });
}

async function cacheJson(cache, url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`缓存 ${url} 失败（${response.status}）`);
  await cache.put(url, response.clone());
}

/** 同一个地址的不同版本（?v=）都要清掉，避免离线包越攒越多。 */
async function deleteByPath(cache, pathname) {
  for (const request of await cache.keys()) {
    if (new URL(request.url).pathname === pathname) await cache.delete(request);
  }
}

/**
 * 下载一章的离线包：转码 m4a → 音频 + 字幕 + 听书目录一起进缓存。
 * force=false 时已经下过且版本一致就直接跳过。
 */
export async function downloadChapter(bookId, chapter, { onProgress, force = false } = {}) {
  if (!offlineSupported()) {
    throw new Error("离线下载需要 HTTPS 访问（局域网 http 只能在线听）");
  }
  await requestPersistentStorage();
  const cache = await caches.open(OFFLINE_CACHE);
  const audioUrl = `/api/books/${bookId}/chapters/${chapter.index}/audio.m4a`;

  if (!force && downloadIsCurrent(bookId, chapter)) {
    const cached = await cache.match(audioUrl, { ignoreSearch: true });
    if (cached) return downloadInfo(bookId, chapter.index);
  }

  onProgress?.({ phase: "prepare", loaded: 0, total: chapter.estimated_bytes || 0 });
  // 只让服务端按需转码（m4a 比 wav 旧才重转）；force 只表示"重新放进离线缓存"
  const prepared = await api.prepareMobile(bookId, chapter.index, { force: false });

  await cacheJson(cache, `/api/books/${bookId}/listen`);
  await cacheJson(cache, `/api/books/${bookId}/chapters/${chapter.index}/subtitles`);

  onProgress?.({ phase: "download", loaded: 0, total: prepared.bytes || chapter.estimated_bytes || 0 });
  // ?fresh=1 让 service worker 绕开旧缓存，保证"重新下载"拿到的是服务器上的新版本
  const response = await fetch(`${audioUrl}?fresh=1`, { cache: "no-store" });
  if (!response.ok) throw new Error(`下载音频失败（${response.status}）`);
  const blob = await readWithProgress(response, prepared.bytes || 0, (loaded, total) =>
    onProgress?.({ phase: "download", loaded, total }),
  );
  await deleteByPath(cache, new URL(audioUrl, location.origin).pathname);
  // 按版本号存：播放器请求 ?v=<mtime> 时能精确命中；版本变了会自动走网络
  await cache.put(
    `${audioUrl}?v=${prepared.mtime}`,
    new Response(blob, {
      status: 200,
      headers: {
        "Content-Type": "audio/mp4",
        "Content-Length": String(blob.size),
        "Accept-Ranges": "bytes",
      },
    }),
  );

  const info = { mtime: prepared.mtime, bytes: blob.size, at: Date.now() };
  const meta = readMeta();
  meta[keyOf(bookId, chapter.index)] = info;
  writeMeta(meta);
  onProgress?.({ phase: "done", loaded: blob.size, total: blob.size });
  return info;
}

/** 整本顺序下载：一章一章来，跳过已是最新的。 */
export async function downloadBook(bookId, chapters, { onProgress, force = false } = {}) {
  const todo = chapters.filter((chapter) => force || !downloadIsCurrent(bookId, chapter));
  let done = 0;
  for (const chapter of todo) {
    await downloadChapter(bookId, chapter, {
      force,
      onProgress: ({ phase, loaded, total }) =>
        onProgress?.({ chapter: chapter.index, done, total: todo.length, phase, loaded, total: total || 0 }),
    });
    done += 1;
    onProgress?.({ chapter: chapter.index, done, total: todo.length, phase: "done", loaded: 1, total: 1 });
  }
  return { done, total: todo.length };
}

export async function removeChapterDownload(bookId, index) {
  const cache = await caches.open(OFFLINE_CACHE);
  await deleteByPath(cache, `/api/books/${bookId}/chapters/${index}/audio.m4a`);
  await deleteByPath(cache, `/api/books/${bookId}/chapters/${index}/subtitles`);
  const meta = readMeta();
  delete meta[keyOf(bookId, index)];
  writeMeta(meta);
}

/** 清空一本的离线包（目录也删；下次在线打开会重新缓存）。 */
export async function removeBookDownloads(bookId, chapters = []) {
  for (const chapter of chapters) {
    await removeChapterDownload(bookId, chapter.index);
  }
  const cache = await caches.open(OFFLINE_CACHE);
  await deleteByPath(cache, `/api/books/${bookId}/listen`);
  const meta = readMeta();
  for (const key of Object.keys(meta)) {
    if (key.startsWith(`${bookId}:`)) delete meta[key];
  }
  writeMeta(meta);
}
