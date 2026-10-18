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

export function greetSkillBundle(skillName: string): Buffer {
  return buildSkillBundleZip({
    "SKILL.md": `---
name: ${skillName}
description: Greets a person by name. To use, run "python scripts/greet.py \\"<name>\\"".
---

# Greet skill

Run \`python scripts/greet.py "<name>"\` to print a greeting for the given name.
`,
    "scripts/greet.py": `import sys
name = sys.argv[1] if len(sys.argv) > 1 else "world"
print(f"Hello, {name}!")
`,
  });
}

/** A bundle deliberately missing SKILL.md — business-backend rejects it
 * with 400 before ever creating a Skill row. */
export function bundleWithoutSkillMd(): Buffer {
  return buildSkillBundleZip({
    "README.md": "This bundle has no SKILL.md and must be rejected.\n",
  });
}
