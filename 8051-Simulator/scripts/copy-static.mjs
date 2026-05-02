import { mkdir, copyFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const root = path.resolve(__dirname, "..");

const distStaticDir = path.join(root, "dist", "api", "static");
const srcStaticDir = path.join(root, "api", "static");

const assetsToCopy = [
  "styles.css",
  "hexlogic-logo.png",
  "hexlogic-logo-light.png",
  "hexlogic-logo-dark.png"
];

await mkdir(distStaticDir, { recursive: true });

for (const name of assetsToCopy) {
  await copyFile(path.join(srcStaticDir, name), path.join(distStaticDir, name));
}

