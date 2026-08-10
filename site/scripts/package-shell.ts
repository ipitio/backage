import { createHash } from "node:crypto";
import { mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { dirname, join, posix, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const manifestName = ".bkg-site-manifest.json";
const dataRootToken = "__BKG_DATA_ROOT__";
const publishPrefix = ".bkg-site/candidate";
const sourceRoot = fileURLToPath(new URL("../build/", import.meta.url));
const outputRoot = fileURLToPath(new URL("../dist/", import.meta.url));
const textExtensions = new Set([".css", ".html", ".js", ".json", ".svg"]);

interface ManifestFile {
  bytes: number;
  path: string;
  sha256: string;
}

function comparePaths(left: string, right: string): number {
  if (left < right) {
    return -1;
  }
  if (left > right) {
    return 1;
  }
  return 0;
}

async function collectFiles(directory: string): Promise<string[]> {
  const entries = await readdir(directory, { withFileTypes: true });
  const paths: string[] = [];

  for (const entry of entries.sort((left, right) =>
    comparePaths(left.name, right.name),
  )) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      paths.push(...(await collectFiles(path)));
    } else if (entry.isFile()) {
      paths.push(path);
    } else {
      throw new Error(`Unsupported Astro output entry: ${path}`);
    }
  }
  return paths;
}

function toPosixPath(path: string): string {
  return path.split(sep).join(posix.sep);
}

function dataRootFor(destination: string): string {
  const parent = posix.dirname(destination);
  const depth = parent === "." ? 0 : parent.split("/").length;
  return depth === 0 ? "./" : "../".repeat(depth);
}

function hydrate(content: Buffer, destination: string): Buffer {
  const extension = posix.extname(destination);
  if (!textExtensions.has(extension)) {
    return content;
  }
  return Buffer.from(
    content.toString("utf8").replaceAll(dataRootToken, dataRootFor(destination)),
  );
}

function digest(content: Buffer): string {
  return createHash("sha256").update(content).digest("hex");
}

async function packageShell(): Promise<void> {
  const sourceFiles = (await collectFiles(sourceRoot)).filter(
    (path) => !path.endsWith(`${sep}.gitkeep`),
  );
  if (sourceFiles.length === 0) {
    throw new Error("Astro produced no site-shell files");
  }

  await rm(outputRoot, { force: true, recursive: true });
  const files: ManifestFile[] = [];
  let hydrated = false;

  for (const source of sourceFiles) {
    const sourcePath = toPosixPath(relative(sourceRoot, source));
    const destination = posix.join(publishPrefix, sourcePath);
    const original = await readFile(source);
    const content = hydrate(original, destination);
    hydrated ||= !content.equals(original);
    const output = resolve(outputRoot, ...destination.split("/"));
    if (!output.startsWith(`${resolve(outputRoot)}${sep}`)) {
      throw new Error(`Site-shell destination escapes output root: ${destination}`);
    }
    await mkdir(dirname(output), { recursive: true });
    await writeFile(output, content);
    files.push({
      bytes: content.byteLength,
      path: destination,
      sha256: digest(content),
    });
  }

  if (!hydrated) {
    throw new Error(`Astro output did not contain ${dataRootToken}`);
  }

  const entrypoint = posix.join(publishPrefix, "index.html");
  if (!files.some((file) => file.path === entrypoint)) {
    throw new Error(`Site shell is missing its entrypoint: ${entrypoint}`);
  }
  const manifest = {
    dashboard_schema_version: 1,
    entrypoint,
    files: files.sort((left, right) => comparePaths(left.path, right.path)),
    schema_version: 1,
    site_shell_version: 1,
  };
  await writeFile(
    join(outputRoot, manifestName),
    `${JSON.stringify(manifest, null, 2)}\n`,
  );
}

await packageShell();
