import { readFile, writeFile, appendFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { gzipSync } from 'node:zlib';

const root = resolve(import.meta.dirname, '..');
const manifest = JSON.parse(await readFile(resolve(root, 'dist/.vite/manifest.json'), 'utf8'));
const initial = new Set();
function include(key) {
  const chunk = manifest[key];
  if (!chunk || initial.has(chunk.file)) return;
  initial.add(chunk.file);
  for (const css of chunk.css ?? []) initial.add(css);
  for (const dependency of chunk.imports ?? []) include(dependency);
}
for (const [key, chunk] of Object.entries(manifest)) if (chunk.isEntry) include(key);
const files = [...new Set(Object.values(manifest).flatMap(chunk => [chunk.file, ...(chunk.css ?? [])]))].sort();
const rows = await Promise.all(files.map(async file => {
  const bytes = await readFile(resolve(root, 'dist', file));
  return { file, raw: bytes.length, gzip: gzipSync(bytes).length, initial: initial.has(file) };
}));
const initialBytes = rows.filter(row => row.initial).reduce((total, row) => total + row.gzip, 0);
// The initial shell excludes lazy feature routes. Keep its compressed transfer
// below 220 KiB; this is a build budget, not a browser performance measurement.
const budget = 220 * 1024;
const report = [
  '# WebUI bundle', '',
  `Initial shell: **${(initialBytes / 1024).toFixed(1)} KiB gzip** (budget: 220 KiB).`, '',
  '| Asset | Raw KiB | Gzip KiB | Initial shell |',
  '| --- | ---: | ---: | :---: |',
  ...rows.map(row => `| ${row.file} | ${(row.raw / 1024).toFixed(1)} | ${(row.gzip / 1024).toFixed(1)} | ${row.initial ? 'yes' : ''} |`), '',
].join('\n');
await writeFile(resolve(root, 'bundle-report.md'), report);
if (process.env.GITHUB_STEP_SUMMARY) await appendFile(process.env.GITHUB_STEP_SUMMARY, report);
console.log(`Initial shell: ${(initialBytes / 1024).toFixed(1)} KiB gzip. Report: web/bundle-report.md`);
if (initialBytes > budget) {
  console.error('Initial WebUI shell exceeds the documented 220 KiB gzip budget.');
  process.exitCode = 1;
}
