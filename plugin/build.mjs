import { copyFile, mkdir, readFile, writeFile } from "node:fs/promises";
import { build } from "esbuild";

const version = (await readFile(new URL("../VERSION", import.meta.url), "utf8")).trim();
if (!/^\d+\.\d+\.\d+$/.test(version)) throw new Error("Invalid version");
await build({
  entryPoints: ["main.ts"],
  bundle: true,
  platform: "node",
  target: "es2022",
  format: "cjs",
  external: ["obsidian"],
  outfile: "dist/main.js",
});
await mkdir("dist", { recursive: true });
await copyFile("styles.css", "dist/styles.css");
await writeFile("dist/manifest.json", JSON.stringify({
  id: "obsidian-thing",
  name: "Obsidian Thing",
  version,
  minAppVersion: "1.13.2",
  description: "Search a vault and explore related notes through an Obsidian Thing server.",
  author: "rokokol",
  isDesktopOnly: true,
}, null, 2) + "\n");
