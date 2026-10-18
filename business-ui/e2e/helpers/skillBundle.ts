import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

/**
 * Builds a real .zip Agent Skills bundle in memory, for feeding into
 * Playwright's setInputFiles({ buffer }) against the real
 * POST /v1/skills/code upload (business-backend/src/services/skill_bundle.py
 * parses a genuine zip with zipfile, so a fake/empty buffer won't pass).
 *
 * Shells out to python3's zipfile module — same approach as
 * smoke-test-code-skill.sh at the repo root — rather than adding a zip
 * npm dependency just for test fixtures.
 */
export function buildSkillBundleZip(
  files: Record<string, string>
): Buffer {
  const dir = mkdtempSync(join(tmpdir(), "e2e-skill-bundle-"));
  try {
    const zipPath = join(dir, "bundle.zip");
    const script = `
import json, os, zipfile

files = json.loads(${JSON.stringify(JSON.stringify(files))})
base = ${JSON.stringify(join(dir, "src"))}
os.makedirs(base, exist_ok=True)
for rel, content in files.items():
    full = os.path.join(base, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as fh:
        fh.write(content)

with zipfile.ZipFile(${JSON.stringify(zipPath)}, "w") as zf:
    for root, _dirs, fnames in os.walk(base):
        for fname in fnames:
            full = os.path.join(root, fname)
            zf.write(full, os.path.relpath(full, base))
`;
    execFileSync("python3", ["-c", script]);
    return readFileSync(zipPath);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

/**
 * Requires two sequential shell commands, where the second command's input
 * depends on the first command's real stdout — only passes with genuine
 * multi-round sandbox execution (CodeSkillSessionWorkflow), not a single
 * fire-and-forget command. Same skill as smoke-test-code-skill.sh's
 * "multistep" scenario.
 */
export function multistepMathSkillBundle(skillName: string): Buffer {
  return buildSkillBundleZip({
    "SKILL.md": `---
name: ${skillName}
description: Computes a two-step result using two sequential shell commands, where the second command's input depends on the first command's output. To use, ask it to compute the two-step result.
---

# Multi-step skill

You must run exactly two commands, one after the other, and read the output
of each before deciding the next command:

1. Run: \`python3 -c "print(6 * 7)"\` — this prints a number N.
2. Run a second, different command: \`python3 -c "print(N + 100)"\`, replacing
   \`N\` with the exact number printed by step 1 (do not guess it, use the
   real printed value).

Do not skip a step. Report the final printed number as your answer, in
exactly this form: \`FINAL RESULT: <number>\`.
`,
  });
}
