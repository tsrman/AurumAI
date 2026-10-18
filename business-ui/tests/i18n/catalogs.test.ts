import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const ROOT = path.resolve(import.meta.dirname, "../..");
type Catalog = { [key: string]: string | Catalog };

function load(locale: string): Catalog {
  return JSON.parse(readFileSync(path.join(ROOT, "messages", `${locale}.json`), "utf8"));
}

function flatten(catalog: Catalog, prefix = ""): Map<string, string> {
  const entries = new Map<string, string>();
  for (const [key, value] of Object.entries(catalog)) {
    const name = prefix ? `${prefix}.${key}` : key;
    if (typeof value === "string") entries.set(name, value);
    else for (const [nested, text] of flatten(value, name)) entries.set(nested, text);
  }
  return entries;
}

// ICU argument names and their types ({count, plural, ...}) must match so the
// same call site works for every locale.
function argumentsOf(message: string): string[] {
  const found = new Set<string>();
  for (const match of message.matchAll(/\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:,\s*([a-z]+))?/g)) {
    found.add(match[2] ? `${match[1]}:${match[2]}` : match[1]);
  }
  for (const match of message.matchAll(/<([A-Za-z][A-Za-z0-9]*)>/g)) found.add(`<${match[1]}>`);
  return [...found].sort();
}

function sourceFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((name) => {
    const full = path.join(directory, name);
    if (statSync(full).isDirectory()) return name === "__tests__" ? [] : sourceFiles(full);
    return /\.(ts|tsx)$/.test(name) && !/\.test\.tsx?$/.test(name) ? [full] : [];
  });
}

describe("message catalogs", () => {
  const ru = flatten(load("ru"));
  const en = flatten(load("en"));

  it("define the same keys in every locale", () => {
    expect([...en.keys()].sort()).toEqual([...ru.keys()].sort());
    expect(ru.size).toBeGreaterThan(0);
  });

  it("use the same ICU arguments and rich-text tags in every locale", () => {
    for (const [key, text] of ru) expect(argumentsOf(en.get(key) ?? ""), key).toEqual(argumentsOf(text));
  });

  it("keep English messages free of Cyrillic text", () => {
    for (const [key, text] of en) expect(/[\u0400-\u04ff]/.test(text), key).toBe(false);
  });

  it("leave no user-visible Russian literals in application source", () => {
    const offenders = sourceFiles(path.join(ROOT, "src")).filter((file) =>
      /[\u0400-\u04ff]/.test(readFileSync(file, "utf8")),
    );
    expect(offenders.map((file) => path.relative(ROOT, file))).toEqual([]);
  });
});
