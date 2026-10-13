import { execFileSync } from "node:child_process";
import { readdirSync } from "node:fs";
import { resolve } from "node:path";
const assets = resolve("../../backend/webui/assets/settings");
for (const file of readdirSync(assets).filter((name) => name.endsWith(".js"))) {
  execFileSync(process.execPath, ["--check", resolve(assets, file)], {
    stdio: "inherit",
  });
}
console.log(
  "Settings JavaScript syntax checks passed. Production serves these modules directly.",
);
