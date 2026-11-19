import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import path from "node:path";

const root = path.resolve(import.meta.dirname, "..");
const output = path.join(root, "dist");
const template = await readFile(path.join(root, "worker", "index.template.js"), "utf8");
const manifest = await readFile(path.join(root, "static", "manifest.webmanifest"), "utf8");
const iconPaths = ["/favicon.ico", "/apple-touch-icon.png", ...JSON.parse(manifest).icons.map(icon => icon.src)];
const iconFiles = {};
for (const iconPath of iconPaths) {
  iconFiles[iconPath] = await readFile(path.join(root, "static", iconPath.slice(1)), "base64");
}
const assets = {
  "__INDEX_HTML__": await readFile(path.join(root, "static", "index.html"), "utf8"),
  "__MANIFEST__": manifest,
  "__ICON__": await readFile(path.join(root, "static", "icon.svg"), "utf8"),
  "__ICON_FILES__": iconFiles,
  "__SERVICE_WORKER__": await readFile(path.join(root, "static", "sw.js"), "utf8")
};

let worker = template;
for (const [placeholder, value] of Object.entries(assets)) {
  worker = worker.replace(placeholder, JSON.stringify(value));
}

await rm(output, { recursive: true, force: true });
await mkdir(path.join(output, "server"), { recursive: true });
await mkdir(path.join(output, ".openai"), { recursive: true });
await writeFile(path.join(output, "server", "index.js"), worker);
await writeFile(
  path.join(output, ".openai", "hosting.json"),
  await readFile(path.join(root, ".openai", "hosting.json"))
);
console.log(`Built ${output}`);
