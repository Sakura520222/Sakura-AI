import { Buffer } from "node:buffer";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { lstat, readdir, readFile, writeFile } from "node:fs/promises";
import { dirname, extname, join, relative, resolve } from "node:path";

const web = resolve(import.meta.dirname, "..");
const root = dirname(web);
const dist = join(web, "dist");
const metadataName = "webui-build.json";
const textExtensions = new Set([
  ".ts",
  ".tsx",
  ".js",
  ".mjs",
  ".css",
  ".html",
  ".json",
  ".svg",
  ".yaml",
  ".yml",
  ".md",
]);
const hash = (data) => createHash("sha256").update(data).digest("hex");
async function files(directory) {
  const result = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isSymbolicLink())
      throw new Error("WebUI assets must not contain symbolic links");
    if (entry.isDirectory()) result.push(...(await files(path)));
    else if (entry.isFile()) result.push(path);
  }
  return result;
}
async function sourceHash() {
  const inputs = [];
  for (const name of [
    "src",
    "public",
    "scripts",
    "index.html",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "vite.config.ts",
    "openapi.json",
  ]) {
    const path = join(web, name);
    let info;
    try {
      info = await lstat(path);
    } catch (error) {
      if (error.code === "ENOENT") continue;
      throw error;
    }
    if (info.isSymbolicLink())
      throw new Error("WebUI source must not contain symbolic links");
    inputs.push(...(info.isDirectory() ? await files(path) : [path]));
  }
  const entries = [];
  for (const path of inputs) {
    let bytes = await readFile(path);
    if (textExtensions.has(extname(path)))
      bytes = Buffer.from(bytes.toString("utf8").replace(/\r\n/g, "\n"));
    entries.push([relative(web, path).replaceAll("\\", "/"), hash(bytes)]);
  }
  entries.sort(([a], [b]) => Buffer.compare(Buffer.from(a), Buffer.from(b)));
  return hash(entries.map(([path, digest]) => `${path}\0${digest}\n`).join(""));
}

const backendSource = await readFile(
  join(root, "backend", "__init__.py"),
  "utf8",
);
const version = backendSource.match(
  /^__version__\s*=\s*["']([^"']+)["']/m,
)?.[1];
if (!version) throw new Error("Missing backend version for WebUI build");
const injectedVersion = process.env.SAKURA_BUILD_VERSION;
if (
  injectedVersion &&
  injectedVersion !== "unknown" &&
  injectedVersion !== version
)
  throw new Error("WebUI/backend version mismatch");
let revision = process.env.SAKURA_BUILD_REVISION || "";
if (!/^[0-9a-f]{40}$/.test(revision)) {
  try {
    revision = execFileSync("git", ["rev-parse", "HEAD"], {
      cwd: root,
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 5000,
    }).trim();
  } catch {
    revision = "";
  }
}
revision = /^[0-9a-f]{40}$/.test(revision) ? revision : null;
const source_hash = await sourceHash();
const inventory = {};
for (const path of (await files(dist)).sort()) {
  const name = relative(dist, path).replaceAll("\\", "/");
  if (name === metadataName) continue;
  const data = await readFile(path);
  inventory[name] = { size: data.length, sha256: hash(data) };
}
if (
  !inventory["index.html"] ||
  !Object.keys(inventory).some((name) => name.startsWith("assets/"))
)
  throw new Error("Incomplete WebUI distribution");
const metadata = {
  schema_version: 1,
  version,
  revision,
  source_hash,
  files: inventory,
};
if (process.argv.includes("--verify")) {
  const saved = JSON.parse(await readFile(join(dist, metadataName), "utf8"));
  if (
    saved.schema_version !== 1 ||
    saved.version !== version ||
    (revision && saved.revision !== revision) ||
    saved.source_hash !== source_hash ||
    JSON.stringify(saved.files) !== JSON.stringify(inventory)
  )
    throw new Error("Prebuilt WebUI does not match this source checkout");
  process.stdout.write(
    `Using prebuilt WebUI ${version} (${revision || "source"})\n`,
  );
} else {
  await writeFile(
    join(dist, metadataName),
    JSON.stringify(metadata, null, 2) + "\n",
  );
  process.stdout.write(
    `WebUI build metadata: ${version} (${revision || "source"})\n`,
  );
}
