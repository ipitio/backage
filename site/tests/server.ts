import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, resolve, sep } from "node:path";

const host = process.env.BKG_SITE_TEST_HOST ?? "127.0.0.1";
const port = 4_173;
const root = resolve("dist");
const candidate = "/.bkg-site/candidate/index.html";
const releaseToken = "__BKG_LATEST_RELEASE_URL__";
const releaseUrl = "https://github.com/example/backage/releases/latest";
const contentTypes: Record<string, string> = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".ico": "image/x-icon",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".webp": "image/webp",
};

const server = createServer(async (request, response) => {
  try {
    const url = new URL(request.url ?? "/", `http://${host}:${port}`);
    const pathname = url.pathname === "/" ? candidate : url.pathname;
    const decoded = decodeURIComponent(pathname).replace(/^\/+/, "");
    const path = resolve(root, decoded);
    if (path !== root && !path.startsWith(`${root}${sep}`)) {
      response.writeHead(400).end("Bad request");
      return;
    }
    let content = await readFile(path);
    if (extname(path) === ".html") {
      content = Buffer.from(
        content.toString("utf8").replaceAll(releaseToken, releaseUrl),
      );
    }
    response.writeHead(200, {
      "cache-control": "no-store",
      "content-type": contentTypes[extname(path)] ?? "application/octet-stream",
    });
    response.end(content);
  } catch {
    response.writeHead(404, { "content-type": "text/plain; charset=utf-8" });
    response.end("Not found");
  }
});

server.listen(port, host);

function close(): void {
  server.close(() => process.exit(0));
}

process.on("SIGINT", close);
process.on("SIGTERM", close);
