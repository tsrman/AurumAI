#!/usr/bin/env node
// Mask runtime literals with the actual TypeScript parser before scanning comments.
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const scriptRoot = path.dirname(fileURLToPath(import.meta.url));
try {
  const require = createRequire(path.resolve(scriptRoot, "package.json"));
  const ts = require(process.env.AURUMAI_TYPESCRIPT_PATH || "typescript");
  const inputs = JSON.parse(fs.readFileSync(0, "utf8"));
  const findings = [];
  for (const { path: name, text } of inputs) {
    const source = ts.createSourceFile(name, text, ts.ScriptTarget.Latest, true);
    const ranges = [];
    function visit(node) {
      if (ts.isStringLiteralLike(node) || ts.isRegularExpressionLiteral(node) ||
          node.kind === ts.SyntaxKind.JsxText || node.kind === ts.SyntaxKind.TemplateHead ||
          node.kind === ts.SyntaxKind.TemplateMiddle || node.kind === ts.SyntaxKind.TemplateTail) {
        ranges.push([node.getStart(source), node.end]);
      }
      ts.forEachChild(node, visit);
    }
    visit(source);
    let masked = text;
    for (const [start, end] of ranges.sort((a, b) => b[0] - a[0])) {
      masked = masked.slice(0, start) + masked.slice(start, end).replace(/[^\r\n]/g, " ") + masked.slice(end);
    }
    const scanner = ts.createScanner(ts.ScriptTarget.Latest, false, ts.LanguageVariant.Standard, masked);
    for (let token = scanner.scan(); token !== ts.SyntaxKind.EndOfFileToken; token = scanner.scan()) {
      if ((token === ts.SyntaxKind.SingleLineCommentTrivia || token === ts.SyntaxKind.MultiLineCommentTrivia) &&
          /[\u0400-\u04ff]/.test(scanner.getTokenText())) {
        findings.push({ path: name, line: source.getLineAndCharacterOfPosition(scanner.getTokenPos()).line + 1, rule: "non_english_prose" });
      }
    }
  }
  process.stdout.write(JSON.stringify(findings));
} catch {
  process.stderr.write("TypeScript comment check failed; raw diagnostics suppressed.\n");
  process.exitCode = 1;
}
