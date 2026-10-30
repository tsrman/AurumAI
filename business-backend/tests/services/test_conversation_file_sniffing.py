import io
import zipfile
from uuid import uuid4

import pytest

from src.services.conversation_files import (
    MAX_FILE_BYTES,
    FileKind,
    FileRejected,
    display_name,
    file_key,
    sniff,
)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _zip(*names: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, "x")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("data", "name", "expected"),
    [
        (b"%PDF-1.7 body", "chart.png", FileKind("application/pdf", "pdf")),
        (_zip("[Content_Types].xml", "xl/workbook.xml"), "data.csv", FileKind(XLSX, "xlsx")),
        (_zip("[Content_Types].xml", "word/document.xml"), "a", FileKind(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"
        )),
        (_zip("[Content_Types].xml", "ppt/presentation.xml"), "a.pptx", FileKind(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"
        )),
        # Office parts without the content-types index are a plain archive.
        (_zip("xl/workbook.xml"), "a.xlsx", FileKind("application/zip", "zip")),
        (b"a,b\n1,2\n", "data.CSV", FileKind("text/csv", "csv")),
        (b"# Title\n", "notes.md", FileKind("text/markdown", "md")),
        ('{"ключ": 1}'.encode(), "data.json", FileKind("application/json", "json")),
        (b"plain\ttext\r\n", "script.py", FileKind("text/plain", "txt")),
        (b"no extension", "README", FileKind("text/plain", "txt")),
    ],
)
def test_the_type_comes_from_the_bytes(data: bytes, name: str, expected: FileKind) -> None:
    assert sniff(data, name) == expected


@pytest.mark.parametrize(
    ("data", "name", "code"),
    [
        (b"", "a.txt", "file_empty"),
        (b"x" * (MAX_FILE_BYTES + 1), "a.txt", "file_too_large"),
        (b"\x89PNG\r\n\x1a\nbytes", "chart.png", "file_type_unsupported"),
        (b"\xff\xfe binary", "a.txt", "file_type_unsupported"),
        (b"text with \x00 nul", "a.txt", "file_type_unsupported"),
        (b"{not json", "a.json", "file_type_unsupported"),
        (b"PK\x03\x04 broken", "a.zip", "file_type_unsupported"),
        (b"MZ\x90\x00", "tool.exe", "file_type_unsupported"),
    ],
)
def test_unrecognised_bytes_are_refused(data: bytes, name: str, code: str) -> None:
    with pytest.raises(FileRejected) as refused:
        sniff(data, name)
    assert refused.value.code == code


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("/workspace/out/../../etc/passwd", "passwd.pdf"),
        ("C:\\Users\\x\\отчёт Q1.xlsx", "отчёт Q1.pdf"),
        ("evil\x00name\x1b.pdf", "evilname.pdf"),
        ("a;b|c`$(rm).pdf", "a_b_c_(rm).pdf"),
        ("...", "file.pdf"),
        ("", "file.pdf"),
    ],
)
def test_the_name_is_a_plain_file_name_with_the_real_extension(name: str, expected: str) -> None:
    assert display_name(name, FileKind("application/pdf", "pdf")) == expected


def test_long_names_are_cut_to_255_characters() -> None:
    assert len(display_name("a" * 1000 + ".csv", FileKind("text/csv", "csv"))) <= 255


def test_the_key_lives_in_the_conversation_files_folder() -> None:
    org, conversation, operation = uuid4(), uuid4(), uuid4()
    key = file_key(org, conversation, operation, FileKind("text/csv", "csv"))
    assert key == f"conversation-images/{org}/{conversation}/files/{operation}.csv"
