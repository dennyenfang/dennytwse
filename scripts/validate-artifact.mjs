import { pathToFileURL } from "node:url";
import path from "node:path";

const target = path.resolve(import.meta.dirname, "..", "dist", "server", "index.js");
const module = await import(`${pathToFileURL(target).href}?v=${Date.now()}`);
if (!module.default || typeof module.default.fetch !== "function") {
  throw new Error("dist/server/index.js must default-export an object with fetch()");
}
console.log("Artifact is valid ESM and exports default.fetch");
