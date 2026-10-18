#!/usr/bin/env node
// Validates every ```mermaid block in the repo's tracked markdown by running it
// through the real mermaid parser — the same one GitHub renders with, so a block
// that fails here is exactly the "Unable to render rich display / Parse error"
// GitHub shows on the blob page.
//
// Parse-only: no headless browser, no image output. mermaid needs a DOM to
// initialize, which jsdom provides; that is the only reason jsdom is a dep.
//
// Usage: node scripts/check-mermaid.mjs [path ...]
//   no args  — every *.md tracked by git (so node_modules/, design/ etc. are
//              excluded for free by .gitignore)
//   paths    — those files/globs only

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { JSDOM } from 'jsdom';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function trackedMarkdown() {
  const out = execFileSync('git', ['ls-files', '*.md'], { cwd: repoRoot, encoding: 'utf8' });
  return out.split('\n').filter(Boolean);
}

// Returns { startLine, code } per block. startLine is the 1-indexed line of the
// opening fence, so a parse error reported on the block's line N maps to
// startLine + N in the file.
function extractBlocks(text) {
  const lines = text.split('\n');
  const blocks = [];
  let current = null;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (current === null) {
      if (/^\s*```+\s*mermaid\s*$/.test(line)) {
        current = { startLine: i + 1, body: [] };
      }
    } else if (/^\s*```+\s*$/.test(line)) {
      blocks.push({ startLine: current.startLine, code: current.body.join('\n') });
      current = null;
    } else {
      current.body.push(line);
    }
  }

  if (current !== null) {
    blocks.push({ startLine: current.startLine, code: current.body.join('\n'), unterminated: true });
  }
  return blocks;
}

function setupDom() {
  const dom = new JSDOM('<!doctype html><html><body></body></html>', { pretendToBeVisual: true });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.HTMLElement = dom.window.HTMLElement;
  globalThis.SVGElement = dom.window.SVGElement;
  // navigator is a getter-only global on modern Node — defineProperty, not assignment.
  Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true });
}

setupDom();
const mermaid = (await import('mermaid')).default;
mermaid.initialize({ startOnLoad: false });

const files = process.argv.length > 2 ? process.argv.slice(2) : trackedMarkdown();
let checked = 0;
const failures = [];

for (const file of files) {
  const abs = path.resolve(repoRoot, file);
  if (!fs.existsSync(abs)) {
    failures.push({ file, message: 'file not found' });
    continue;
  }

  for (const block of extractBlocks(fs.readFileSync(abs, 'utf8'))) {
    const rel = path.relative(repoRoot, abs);
    if (block.unterminated) {
      failures.push({ file: `${rel}:${block.startLine}`, message: 'unterminated ```mermaid block' });
      continue;
    }
    checked++;
    try {
      await mermaid.parse(block.code);
    } catch (error) {
      const message = String(error?.message ?? error);
      // mermaid reports "Parse error on line N" relative to the block body.
      const inBlock = message.match(/on line (\d+)/);
      const line = inBlock ? block.startLine + Number(inBlock[1]) : block.startLine;
      failures.push({ file: `${rel}:${line}`, message });
    }
  }
}

if (failures.length > 0) {
  for (const failure of failures) {
    console.error(`FAIL ${failure.file}`);
    console.error(`${failure.message}\n`);
  }
  console.error(`${failures.length} of ${checked} mermaid block(s) failed to parse.`);
  process.exit(1);
}

console.log(`OK — ${checked} mermaid block(s) in ${files.length} markdown file(s) parse cleanly.`);
