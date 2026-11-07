#!/usr/bin/env node

import { lookup } from "node:dns/promises";
import { isIP } from "node:net";

const port = 9222;
const action = process.argv[2];
const input = process.argv[3] ? JSON.parse(process.argv[3]) : {};
const base = `http://127.0.0.1:${port}`;

async function pages() {
  const response = await fetch(`${base}/json/list`, { redirect: "error", signal: AbortSignal.timeout(3000) });
  if (!response.ok) throw new Error(`CDP tab list returned HTTP ${response.status}`);
  const value = await response.json();
  return value.filter((target) => target.type === "page");
}

function isPublicIPv4(address) {
  const parts = address.split(".").map(Number);
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return false;
  const [a, b, c] = parts;
  if (a === 0 || a === 10 || a === 127 || a >= 224) return false;
  if (a === 169 && b === 254) return false;
  if (a === 172 && b >= 16 && b <= 31) return false;
  if (a === 192 && b === 168) return false;
  if (a === 100 && b >= 64 && b <= 127) return false;
  if (a === 192 && b === 0 && c === 0) return false;
  if (a === 192 && b === 88 && c === 99) return false;
  if (a === 192 && b === 0 && c === 2) return false;
  if (a === 198 && (b === 18 || b === 19)) return false;
  if (a === 198 && b === 51 && c === 100) return false;
  if (a === 203 && b === 0 && c === 113) return false;
  if (a === 255) return false;
  return true;
}

function isPublicIPv6(address) {
  const value = address.toLowerCase().split("%")[0];
  if (value.startsWith("::ffff:")) return isPublicIPv4(value.slice(7));
  const words = value.split(":");
  const first = Number.parseInt(words[0] || "0", 16);
  const second = Number.parseInt(words[1] || "0", 16);
  return (first & 0xe000) === 0x2000
    && !(value.startsWith("2001:") && second <= 0x01ff)
    && !value.startsWith("2001:2:")
    && !value.startsWith("2001:10:")
    && !value.startsWith("2001:20:")
    && !value.startsWith("2001:db8:")
    && !value.startsWith("2002:");
}

async function isPublicHttpUrl(rawUrl) {
  try {
    const url = new URL(rawUrl);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return false;
    const hostname = url.hostname.replace(/^\[|\]$/g, "");
    const normalizedHost = hostname.replace(/\.$/, "").toLowerCase();
    if (normalizedHost === "localhost" || /\.(localhost|local|internal|home|lan)$/.test(normalizedHost)) return false;
    const ipVersion = isIP(hostname);
    if (ipVersion === 4) return isPublicIPv4(hostname);
    if (ipVersion === 6) return isPublicIPv6(hostname);
    const records = await lookup(hostname, { all: true, verbatim: true });
    return records.length > 0 && records.every((record) => record.family === 4 ? isPublicIPv4(record.address) : isPublicIPv6(record.address));
  } catch {
    return false;
  }
}

async function createPage() {
  const response = await fetch(`${base}/json/new?about:blank`, { method: "PUT", redirect: "error", signal: AbortSignal.timeout(3000) });
  if (!response.ok) throw new Error(`CDP could not create a blank tab (HTTP ${response.status})`);
  const target = await response.json();
  if (target.type !== "page" || !target.webSocketDebuggerUrl) throw new Error("CDP returned an invalid page target");
  return target;
}

function connect(wsUrl) {
  const parsed = new URL(wsUrl);
  if (parsed.protocol !== "ws:" || parsed.hostname !== "127.0.0.1" || parsed.port !== String(port)) {
    throw new Error("CDP returned a WebSocket outside the expected loopback endpoint");
  }
  const socket = new WebSocket(wsUrl);
  const pending = new Map();
  const eventListeners = new Set();
  let nextId = 1;
  socket.addEventListener("message", (event) => {
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    if (message.method) {
      for (const listener of eventListeners) listener(message);
      return;
    }
    if (!message.id || !pending.has(message.id)) return;
    const request = pending.get(message.id);
    pending.delete(message.id);
    clearTimeout(request.timer);
    if (message.error) request.reject(new Error(message.error.message || "CDP command failed"));
    else request.resolve(message.result || {});
  });
  const opened = new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("CDP WebSocket connection failed")), { once: true });
  });
  return {
    socket,
    async ready() { await opened; },
    command(method, params = {}) {
      const id = nextId++;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pending.delete(id);
          reject(new Error(`CDP command timed out: ${method}`));
        }, 8000);
        pending.set(id, { resolve, reject, timer });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
    onEvent(listener) { eventListeners.add(listener); },
    close() { socket.close(); },
  };
}

function selectPage(allPages, selector = "") {
  if (!allPages.length) throw new Error("No page is open in the dedicated Chrome profile");
  if (!selector) return allPages[0];
  const index = Number.parseInt(selector, 10);
  if (Number.isInteger(index) && index >= 0 && index < allPages.length) return allPages[index];
  const needle = selector.toLocaleLowerCase();
  const found = allPages.find((page) => `${page.title || ""}\n${page.url || ""}`.toLocaleLowerCase().includes(needle));
  if (!found) throw new Error("No browser tab matched the requested selector");
  return found;
}

async function evaluate(page, expression) {
  const client = connect(page.webSocketDebuggerUrl);
  try {
    await client.ready();
    const result = await client.command("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (result.exceptionDetails) throw new Error("Page snapshot evaluation failed");
    return result.result?.value;
  } finally {
    client.close();
  }
}

async function main() {
  if (action === "tabs") {
    const data = await Promise.all((await pages()).map(async ({ title, url, targetId }) => {
      const allowed = await isPublicHttpUrl(url);
      return { title: allowed ? title : "[private/local tab hidden]", url: allowed ? url : "[blocked]", targetId };
    }));
    process.stdout.write(JSON.stringify({ success: true, action, data }));
    return;
  }
  if (action !== "snapshot" && action !== "open" && action !== "click-link") throw new Error("unsupported browser action");
  const allPages = await pages();
  const page = action === "open" && allPages.length === 0 ? await createPage() : selectPage(allPages, input.tab || "");
  let navigationUrl = action === "open" ? input.url : "";
  if (action === "click-link") {
    if (!await isPublicHttpUrl(page.url)) throw new Error("clicking on private/local browser pages is blocked");
    const id = Number(input.element_id);
    if (!Number.isInteger(id) || id < 1 || id > 20) throw new Error("element_id must identify an element from the latest snapshot");
    navigationUrl = await evaluate(page, `(() => {
      const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
      const nodes = [...document.querySelectorAll('a,button,input,textarea,select,[role="button"],[role="link"]')].filter(visible).slice(0, 20);
      const target = nodes[${id - 1}];
      return target instanceof HTMLAnchorElement ? target.href : '';
    })()`);
    if (typeof navigationUrl !== "string" || !navigationUrl || !await isPublicHttpUrl(navigationUrl)) {
      throw new Error("only an observed public link from the latest snapshot can be followed");
    }
  }
  if (action === "open" || action === "click-link") {
    const target = new URL(navigationUrl);
    if (!await isPublicHttpUrl(target.href)) throw new Error("only public HTTP(S) URLs are allowed");
    const client = connect(page.webSocketDebuggerUrl);
    const pendingInterceptions = new Set();
    try {
      await client.ready();
      await client.command("Fetch.enable", { patterns: [{ urlPattern: "*", requestStage: "Request" }] });
      client.onEvent((event) => {
        if (event.method !== "Fetch.requestPaused") return;
        const requestId = event.params?.requestId;
        const requestUrl = event.params?.request?.url || "";
        const interception = isPublicHttpUrl(requestUrl).then((allowed) => client.command(
          allowed ? "Fetch.continueRequest" : "Fetch.failRequest",
          allowed ? { requestId } : { requestId, errorReason: "BlockedByClient" },
        ));
        pendingInterceptions.add(interception);
        void interception.finally(() => pendingInterceptions.delete(interception)).catch(() => {});
      });
      await client.command("Emulation.setScriptExecutionDisabled", { value: true });
      await client.command("Page.enable");
      const loaded = new Promise((resolve) => {
        const timeout = setTimeout(resolve, 8000);
        client.onEvent((event) => {
          if (event.method === "Page.loadEventFired") {
            clearTimeout(timeout);
            resolve();
          }
        });
      });
      const result = await client.command("Page.navigate", { url: target.href });
      if (result.errorText) throw new Error(result.errorText);
      await loaded;
      while (pendingInterceptions.size) await Promise.allSettled([...pendingInterceptions]);
    } finally {
      client.close();
    }
  }
  if ((action === "snapshot" || action === "click-link") && !await isPublicHttpUrl(page.url)) throw new Error("reading private/local browser pages is blocked");
  const value = await evaluate(page, `(() => {
    const visible = (el) => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
    const nodes = [...document.querySelectorAll('a,button,input,textarea,select,[role="button"],[role="link"]')].filter(visible).slice(0, 20);
    return {
      url: location.href,
      title: document.title,
      text: (document.body?.innerText || '').slice(0, 4000),
      elements: nodes.map((el, index) => ({
        id: String(index + 1), tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || '',
        text: (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim().slice(0, 120),
        href: el instanceof HTMLAnchorElement ? el.href.slice(0, 250) : '',
        inputType: el instanceof HTMLInputElement ? el.type : ''
      }))
    };
  })()`);
  if (!await isPublicHttpUrl(value?.url)) throw new Error("reading private/local browser pages is blocked");
  process.stdout.write(JSON.stringify({ success: true, action, data: value }));
}

main().catch((error) => {
  process.stderr.write(`${error.name || "Error"}: ${error.message || error}\n`);
  process.exitCode = 1;
});
