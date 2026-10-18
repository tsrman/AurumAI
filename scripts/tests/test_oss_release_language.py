from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

# Cyrillic samples are written as escapes so this file itself stays free of
# Russian text, which is exactly what the checker under test enforces.
RU = "\u0442\u0435\u043a\u0441\u0442"


def checker():
    scripts = Path(__file__).resolve().parents[1]
    assert (scripts / "oss_release/language.py").is_file(), "Language checker is not implemented"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    return importlib.import_module("oss_release.language")


def write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.mark.parametrize("name,text", [
    ("main.py", f"# {RU}\nanswer = 'value'\n"),
    ("main.py", f'def function():\n    """{RU}."""\n    return "value"\n'),
    ("main.ts", f'// {RU}\nconst label = "value";\n'),
    ("main.tsx", f'const element = <div>{{/* {RU} */}}value</div>;\n'),
    ("script.sh", f'#!/bin/bash\n# {RU}\nprintf "%s" "value"\n'),
    ("migration.sql", f"-- {RU}\nSELECT 'value';\n"),
    ("compose.yml", f"# {RU}\nname: app\n"),
    ("Dockerfile", f"# {RU}\nFROM python:3.12\n"),
    ("README.md", f"# AurumAI\n{RU}\n"),
])
def test_russian_prose_in_comments_is_reported(tmp_path: Path, name: str, text: str):
    write(tmp_path, name, text)
    findings = checker().check_language(tmp_path, (name,))
    assert [f.rule for f in findings] == ["non_english_prose"]


@pytest.mark.parametrize("name,text", [
    ("main.py", f'answer = "{RU} # string"\n'),
    ("main.ts", f'const label = "{RU} // string";\n'),
    ("main.tsx", f"const element = <div>{RU}</div>;\n"),
    ("script.sh", f'label="{RU} # string"\nprintf "%s" "${{#label}}"\n'),
    ("migration.sql", f"SELECT '{RU} -- string';\n"),
    ("compose.yml", f'name: "{RU} # string"\n'),
    ("realm.json", f'{{"org_name": "{RU}"}}\n'),
    ("business-ui/messages/en.json", f'{{"title": "{RU}"}}\n'),
])
def test_russian_runtime_strings_are_reported(tmp_path: Path, name: str, text: str):
    write(tmp_path, name, text)
    findings = checker().check_language(tmp_path, (name,))
    assert [(f.line, f.rule) for f in findings] == [(1, "non_english_string")]


@pytest.mark.parametrize("name", ["business-ui/messages/ru.json", "admin-ui/messages/ru.json"])
def test_russian_locale_catalogs_are_the_only_allowed_russian_text(tmp_path: Path, name: str):
    write(tmp_path, name, f'{{"title": "{RU}"}}\n')
    assert checker().check_language(tmp_path, (name,)) == ()


def test_a_line_is_reported_once(tmp_path: Path):
    write(tmp_path, "main.py", f'answer = "{RU}"  # {RU}\n')
    findings = checker().check_language(tmp_path, ("main.py",))
    assert [(f.line, f.rule) for f in findings] == [(1, "non_english_prose")]


def test_embedded_python_comments_are_checked(tmp_path: Path):
    write(tmp_path, "main.py", f'PROGRAM = """import json\n# {RU}\nprint(1)\n"""\n')
    findings = checker().check_language(tmp_path, ("main.py",))
    assert [(f.line, f.rule) for f in findings] == [(2, "non_english_prose")]


def test_escaped_cyrillic_is_ascii_source(tmp_path: Path):
    write(tmp_path, "test_sample.py", 'SAMPLE = "\\u0442\\u0435\\u043a\\u0441\\u0442"\n')
    assert checker().check_language(tmp_path, ("test_sample.py",)) == ()
