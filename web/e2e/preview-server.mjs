/* global Buffer, console, fetch */

import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, isAbsolute, join, normalize, relative, resolve, sep } from "node:path";
import { argv } from "node:process";
import { fileURLToPath } from "node:url";

function option(name, fallback) {
  const index = argv.indexOf(name);
  return index >= 0 && argv[index + 1] ? argv[index + 1] : fallback;
}

const host = option("--host", "127.0.0.1");
const port = Number(option("--port", "4173"));
const dist = resolve(option("--dist", "dist"));
const apiBase = option("--api", "http://127.0.0.1:8000").replace(/\/$/, "");
const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

function sendFile(response, filePath) {
  response.statusCode = 200;
  response.setHeader("content-type", contentTypes[extname(filePath)] ?? "application/octet-stream");
  createReadStream(filePath).pipe(response);
}

export function resolveStaticCandidate(distDirectory, requestedPath) {
  const distRoot = resolve(distDirectory);
  let decodedPath;
  try {
    decodedPath = decodeURIComponent(requestedPath.split("?")[0]);
  } catch {
    return join(distRoot, "index.html");
  }
  const relativeRequest = decodedPath.replace(/^[/\\]+/, "");
  const candidate = resolve(distRoot, relativeRequest);
  const relativeCandidate = relative(distRoot, candidate);
  const escapesDist = relativeCandidate === ".."
    || relativeCandidate.startsWith(`..${sep}`)
    || isAbsolute(relativeCandidate);
  return escapesDist ? join(distRoot, "index.html") : candidate;
}

async function proxyApi(request, response) {
  const target = `${apiBase}${request.url.slice("/api".length)}`;
  const headers = { ...request.headers };
  delete headers.host;
  const body = request.method === "GET" || request.method === "HEAD" ? undefined : await new Promise((resolveBody, reject) => {
    const chunks = [];
    request.on("data", (chunk) => chunks.push(chunk));
    request.on("end", () => resolveBody(Buffer.concat(chunks)));
    request.on("error", reject);
  });
  const upstream = await fetch(target, { method: request.method, headers, body });
  response.statusCode = upstream.status;
  upstream.headers.forEach((value, key) => response.setHeader(key, value));
  response.end(Buffer.from(await upstream.arrayBuffer()));
}

const server = createServer(async (request, response) => {
  try {
    if (request.url?.startsWith("/api/")) {
      await proxyApi(request, response);
      return;
    }
    const requested = (request.url ?? "/").split("?")[0];
    const candidate = resolveStaticCandidate(dist, requested === "/" ? "/index.html" : requested);
    const safeCandidate = normalize(candidate);
    if (existsSync(safeCandidate) && statSync(safeCandidate).isFile()) {
      sendFile(response, safeCandidate);
      return;
    }
    sendFile(response, join(dist, "index.html"));
  } catch {
    response.statusCode = 502;
    response.setHeader("content-type", "text/plain; charset=utf-8");
    response.end("Local preview server error");
  }
});

if (argv[1] && resolve(argv[1]) === fileURLToPath(import.meta.url)) {
  server.listen(port, host, () => console.log(`P4_E2E_WEB_READY http://${host}:${port}`,));
}
