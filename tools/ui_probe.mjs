// UI 探针：用 Chrome DevTools Protocol 打开本地界面，等 JS 渲染完，
// 输出关键结构（JSON 到 stdout）并截图。用于 M4 的人工/自动验收。
//
// 用法：
//   node tools/ui_probe.mjs http://127.0.0.1:8300/#/shelf docs/ui-shots/shelf 1440 900
//   node tools/ui_probe.mjs <url> <outPrefix> 390 844 --click=.btn-primary
import { spawn } from "node:child_process";
import { mkdir, readFile, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const [url, outPrefix, widthArg, heightArg, ...rest] = process.argv.slice(2);
if (!url || !outPrefix) {
  console.error("用法: node tools/ui_probe.mjs <url> <outPrefix> [width] [height] [--click=selector ...]");
  process.exit(2);
}
const width = Number(widthArg || 1440);
const height = Number(heightArg || 900);
const clicks = rest.filter((flag) => flag.startsWith("--click=")).map((flag) => flag.slice("--click=".length));
const viewportOnly = rest.includes("--viewport");
const scrollTo = rest.find((flag) => flag.startsWith("--scroll-to="))?.slice("--scroll-to=".length) || null;
const extraEvals = rest.filter((flag) => flag.startsWith("--eval=")).map((flag) => flag.slice("--eval=".length));
const stepsFile = rest.find((flag) => flag.startsWith("--steps="))?.slice("--steps=".length) || null;
const settle = Number(rest.find((flag) => flag.startsWith("--wait="))?.slice("--wait=".length) || 2500);
const chromePath = process.env.CHROME_PATH || "C:/Program Files/Google/Chrome/Application/chrome.exe";
const port = 9300 + Math.floor(Math.random() * 400);
const profile = path.join(tmpdir(), `aiab-ui-probe-${port}`);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitForDevtools(timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      const targets = await response.json();
      const page = targets.find((target) => target.type === "page");
      if (page) return page;
    } catch {
      /* 还没起来 */
    }
    await sleep(250);
  }
  throw new Error("Chrome DevTools 端口未就绪");
}

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.id = 0;
    this.pending = new Map();
    this.events = [];
    socket.addEventListener("message", (event) => {
      const payload = JSON.parse(event.data);
      if (payload.id && this.pending.has(payload.id)) {
        const { resolve, reject } = this.pending.get(payload.id);
        this.pending.delete(payload.id);
        if (payload.error) reject(new Error(payload.error.message));
        else resolve(payload.result);
      } else if (payload.method) {
        this.events.push(payload);
      }
    });
  }

  send(method, params = {}) {
    const id = ++this.id;
    this.socket.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`CDP 超时: ${method}`));
        }
      }, 30000);
    });
  }

  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.exception?.description || "页面脚本抛错");
    }
    return result.result?.value;
  }
}

const chrome = spawn(
  chromePath,
  [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    `--window-size=${width},${height}`,
    "about:blank",
  ],
  { stdio: "ignore" },
);

let exitCode = 0;
try {
  const target = await waitForDevtools();
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", reject, { once: true });
  });
  const cdp = new Cdp(socket);
  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Log.enable");
  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile: width <= 720,
  });
  await cdp.send("Page.navigate", { url });
  await sleep(settle);
  for (const selector of clicks) {
    const clicked = await cdp.evaluate(
      `(() => { const node = document.querySelector(${JSON.stringify(selector)});
         if (!node) return false; node.click(); return true; })()`,
    );
  if (!clicked) console.error(`点击失败，找不到元素: ${selector}`);
    await sleep(1200);
  }
  if (scrollTo) {
    await cdp.evaluate(
      `(() => { const node = document.querySelector(${JSON.stringify(scrollTo)});
         if (node) node.scrollIntoView({ block: "start" }); return Boolean(node); })()`,
    );
    await sleep(400);
  }
  if (stepsFile) {
    const steps = JSON.parse(await readFile(stepsFile, "utf8"));
    for (const step of steps) {
      if (step.click) {
        const ok = await cdp.evaluate(
          `(() => { const node = document.querySelector(${JSON.stringify(step.click)});
             if (!node) return false; node.click(); return true; })()`,
        );
        if (!ok) console.error(`步骤点击失败: ${step.click}`);
      }
      if (step.eval) await cdp.evaluate(step.eval);
      await sleep(step.wait ?? 600);
    }
  }

  const summary = await cdp.evaluate(`(() => {
    const text = (document.getElementById("main")?.innerText || "").replace(/\\s+/g, " ").trim();
    return {
      view: document.getElementById("main")?.dataset.view || null,
      boot: document.getElementById("main")?.dataset.boot || null,
      crumb: document.getElementById("crumb")?.textContent || null,
      status: document.getElementById("runlabel")?.textContent || null,
      text: text.slice(0, 600),
      counts: {
        sheets: document.querySelectorAll(".sheet").length,
        books: document.querySelectorAll(".book-card").length,
        rows: document.querySelectorAll(".table tbody tr").length,
        proofLines: document.querySelectorAll(".proof-line").length,
        seals: document.querySelectorAll(".seal").length,
        jobs: document.querySelectorAll(".job-row").length,
        issues: document.querySelectorAll(".issue-row").length,
        voices: document.querySelectorAll(".voice-card").length,
        fields: document.querySelectorAll("[data-key]").length,
        sceneChips: document.querySelectorAll(".scene-chip").length,
        chapterItems: document.querySelectorAll(".chapter-item").length,
        lines: document.querySelectorAll(".line").length,
        castRows: document.querySelectorAll(".cast-row").length,
        pickers: document.querySelectorAll(".picker").length,
        pickerRows: document.querySelectorAll(".picker__row").length,
        rawParagraphs: document.querySelectorAll(".raw-text").length,
      },
      errorBlock: document.querySelector(".error-block")?.innerText?.slice(0, 200) || null,
    };
  })()`);
  for (const expression of extraEvals) {
    summary[`eval:${expression.slice(0, 40)}`] = await cdp.evaluate(expression);
  }
  const consoleErrors = cdp.events
    .filter(
      (event) =>
        (event.method === "Runtime.consoleAPICalled" && event.params.type === "error") ||
        event.method === "Runtime.exceptionThrown" ||
        (event.method === "Log.entryAdded" && event.params.entry.level === "error"),
    )
    .map((event) => {
      if (event.method === "Runtime.exceptionThrown") return event.params.exceptionDetails?.exception?.description || "exception";
      if (event.method === "Log.entryAdded") return event.params.entry.text;
      return event.params.args?.map((arg) => arg.value ?? arg.description ?? "").join(" ");
    });
  const shot = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: !viewportOnly });
  await mkdir(path.dirname(outPrefix), { recursive: true });
  const png = `${outPrefix}.png`;
  await writeFile(png, Buffer.from(shot.data, "base64"));
  console.log(JSON.stringify({ url, width, height, screenshot: png, ...summary, consoleErrors }, null, 2));
  socket.close();
} catch (error) {
  console.error(`探针失败: ${error.message}`);
  exitCode = 1;
} finally {
  chrome.kill();
  await sleep(300);
  await rm(profile, { recursive: true, force: true }).catch(() => {});
}

process.exit(exitCode);
