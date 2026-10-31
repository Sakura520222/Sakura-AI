import { readFile, readdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
import ts from 'typescript';

const root = resolve(import.meta.dirname, '../src');
const pairs = new Map();
const migrate = process.argv.includes('--migrate-templates');
async function walk(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) { await walk(path); continue; }
    if (!/\.tsx?$/.test(entry.name) || /\.test\./.test(entry.name) || entry.name === 'generated.ts') continue;
    let source = await readFile(path, 'utf8');
    let tree = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true);
    const edits = [];
    function migrateNode(node) {
      if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 't' && node.arguments.length === 2 && node.arguments.every(ts.isTemplateExpression)) {
        const [zh, en] = node.arguments;
        const values = zh.templateSpans.map(span => span.expression.getText(tree));
        if (values.length === en.templateSpans.length && en.templateSpans.every((span, index) => span.expression.getText(tree) === values[index])) {
          const template = value => value.head.text + value.templateSpans.map((span, index) => `{${index}}${span.literal.text}`).join('');
          edits.push({ start: node.getStart(tree), end: node.end, replacement: `t(${JSON.stringify(template(zh))}, ${JSON.stringify(template(en))}, [${values.join(', ')}])` });
          return;
        }
      }
      ts.forEachChild(node, migrateNode);
    }
    if (migrate) {
      migrateNode(tree);
      for (const edit of edits.sort((a, b) => b.start - a.start)) source = source.slice(0, edit.start) + edit.replacement + source.slice(edit.end);
      if (edits.length) { await writeFile(path, source); tree = ts.createSourceFile(path, source, ts.ScriptTarget.Latest, true); }
    }
    function add(zh, en) {
      const sourceKey = JSON.stringify([zh, en]);
      pairs.set(sourceKey, { key: `m_${createHash('sha256').update(sourceKey).digest('hex').slice(0, 12)}`, sourceKey, zh, en });
    }
    function visit(node) {
      if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === 't' && node.arguments.length >= 2 && ts.isStringLiteral(node.arguments[0]) && ts.isStringLiteral(node.arguments[1])) add(node.arguments[0].text, node.arguments[1].text);
      // Shared enum labels are passed as t(...labels).
      if (ts.isArrayLiteralExpression(node) && node.elements.length === 2 && node.elements.every(ts.isStringLiteral) && /\p{Script=Han}/u.test(node.elements[0].text)) add(node.elements[0].text, node.elements[1].text);
      // Setup descriptors carry a field key followed by the bilingual label.
      if (ts.isArrayLiteralExpression(node) && node.elements.length >= 3 && node.elements.slice(0, 3).every(ts.isStringLiteral) && /^[A-Z_]+$/.test(node.elements[0].text) && /\p{Script=Han}/u.test(node.elements[1].text)) add(node.elements[1].text, node.elements[2].text);
      ts.forEachChild(node, visit);
    }
    visit(tree);
  }
}
await walk(root);
process.stdout.write(JSON.stringify([...pairs.values()].sort((a, b) => a.key.localeCompare(b.key))));
