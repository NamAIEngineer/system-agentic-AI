#!/usr/bin/env node

import http from "node:http";
import net from "node:net";
import { lookup } from "node:dns/promises";
import { pathToFileURL } from "node:url";

const HOST = "127.0.0.1";
const PORT = 9223;
const server = http.createServer();

function ipv4Value(address) {
  const parts = address.split(".").map(Number);
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255)) return null;
  return parts.reduce((value, part) => (value << 8n) | BigInt(part), 0n);
}

function ipv6Words(address) {
  let value = address.toLowerCase().split("%")[0];
  if (value.includes(".")) {
    const split = value.lastIndexOf(":");
    const v4 = ipv4Value(value.slice(split + 1));
    if (v4 === null) return null;
    value = `${value.slice(0, split)}:${Number((v4 >> 16n) & 0xffffn).toString(16)}:${Number(v4 & 0xffffn).toString(16)}`;
  }
  if ((value.match(/::/g) || []).length > 1) return null;
  const [leftRaw, rightRaw] = value.split("::");
  const left = leftRaw ? leftRaw.split(":") : [];
  const right = rightRaw ? rightRaw.split(":") : [];
  if ([...left, ...right].some((part) => !/^[0-9a-f]{1,4}$/.test(part))) return null;
  const missing = 8 - left.length - right.length;
  if (value.includes("::") ? missing < 1 : missing !== 0) return null;
  return [...left, ...Array(missing).fill("0"), ...right].map((part) => Number.parseInt(part, 16));
}

export function isPublicAddress(address) {
  const family = net.isIP(address);
  if (family === 4) {
    const value = ipv4Value(address);
    if (value === null) return false;
    const ranges = [
      ["0.0.0.0", 8], ["10.0.0.0", 8], ["100.64.0.0", 10], ["127.0.0.0", 8],
      ["169.254.0.0", 16], ["172.16.0.0", 12], ["192.0.0.0", 24], ["192.0.2.0", 24],
      ["192.88.99.0", 24], ["192.168.0.0", 16], ["198.18.0.0", 15], ["198.51.100.0", 24],
      ["203.0.113.0", 24], ["224.0.0.0", 4], ["240.0.0.0", 4],
    ];
    return !ranges.some(([network, prefix]) => {
      const base = ipv4Value(network);
      const shift = BigInt(32 - prefix);
      return (value >> shift) === (base >> shift);
    });
  }
  if (family !== 6) return false;
  const words = ipv6Words(address);
  if (!words) return false;
  if (words.slice(0, 5).every((word) => word === 0) && words[5] === 0xffff) {
    const mapped = `${words[6] >> 8}.${words[6] & 255}.${words[7] >> 8}.${words[7] & 255}`;
    return isPublicAddress(mapped);
  }
  const value = words.reduce((acc, word) => (acc << 16n) | BigInt(word), 0n);
  if ((value >> 125n) !== 1n) return false; // 2000::/3 global unicast only
  if (value >> 105n === 0x200100n) return false; // 2001:0000::/23 special-purpose
  if (value >> 112n === 0x2002n) return false; // 2002::/16 6to4
  if (value >> 96n === 0x20010db8n) return false; // documentation
  if (value >> 108n === 0x3fff0n) return false; // 3fff::/20 documentation
  return true;
}

export async function resolvePinned(hostname, resolver = lookup) {
  const host = hostname.replace(/^\[|\]$/g, "").replace(/\.$/, "").toLowerCase();
  if (!host || host === "localhost" || /\.(localhost|local|internal|home|lan)$/.test(host)) throw new Error("local host blocked");
  const records = net.isIP(host)
    ? [{ address: host, family: net.isIP(host) }]
    : await resolver(host, { all: true, verbatim: true });
  if (!records.length || records.some((record) => !isPublicAddress(record.address))) throw new Error("non-public or mixed DNS answers blocked");
  return records[0];
}

function safeHeaders(headers, host) {
  const out = { ...headers, host };
  for (const key of Object.keys(out)) {
    if (["proxy-authorization", "proxy-connection", "connection", "keep-alive", "transfer-encoding"].includes(key.toLowerCase())) delete out[key];
  }
  return out;
}

server.on("request", async (request, response) => {
  if (request.url === "/__health" && request.method === "GET") {
    response.writeHead(200, { "content-type": "text/plain", "cache-control": "no-store" }).end("ok");
    return;
  }
  try {
    if (!["GET", "HEAD"].includes(request.method) || request.headers["transfer-encoding"] || Number(request.headers["content-length"] || 0) !== 0) {
      throw new Error("proxy allows only bodyless GET/HEAD for plain HTTP");
    }
    const url = new URL(request.url);
    if (url.protocol !== "http:" || url.username || url.password || !["", "80"].includes(url.port)) throw new Error("only public HTTP port 80 is allowed");
    const pinned = await resolvePinned(url.hostname);
    const upstream = http.request({
      host: pinned.address, family: pinned.family, port: 80, method: request.method,
      path: `${url.pathname}${url.search}`, headers: safeHeaders(request.headers, url.host), agent: false,
    }, (upstreamResponse) => {
      response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
      upstreamResponse.pipe(response);
    });
    upstream.on("error", () => { if (!response.headersSent) response.writeHead(502); response.end(); });
    request.on("aborted", () => upstream.destroy());
    upstream.end();
  } catch (error) {
    response.writeHead(403, { "content-type": "text/plain", "cache-control": "no-store" }).end(error.message || "blocked");
  }
});

server.on("connect", async (request, client, head) => {
  try {
    const authority = new URL(`http://${request.url}`);
    if (authority.username || authority.password || !authority.hostname || !["", "443"].includes(authority.port)) throw new Error("only HTTPS port 443 is allowed");
    const pinned = await resolvePinned(authority.hostname);
    const upstream = net.connect({ host: pinned.address, family: pinned.family, port: 443 });
    upstream.once("connect", () => {
      client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (head.length) upstream.write(head);
      client.pipe(upstream);
      upstream.pipe(client);
    });
    upstream.on("error", () => client.destroy());
    client.on("error", () => upstream.destroy());
    client.on("close", () => upstream.destroy());
  } catch {
    client.write("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n");
    client.destroy();
  }
});

server.on("clientError", (_error, socket) => socket.end("HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n"));
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) server.listen(PORT, HOST);
