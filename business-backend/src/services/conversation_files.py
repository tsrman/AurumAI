"""Files an agent attaches to its answer (spreadsheets, documents, data).

The bytes are the agent's, so their type comes from the bytes alone, never
from a name or extension the model chose: a fixed set of formats is
recognised, everything else is refused. Pictures are not files here; they
become conversation images. The name is only shown and offered on download,
reduced to a plain file name.
"""

import io
import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from uuid import UUID

# One spreadsheet or report, well below the archive's object limit.
MAX_FILE_BYTES = 20 * 1024 * 1024
# Attached by one turn, across all of its executions.
MAX_FILES_PER_TURN = 10
_OFFICE = {
    "xl/": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "word/": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "ppt/": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx"),
}
_TEXT_EXTENSIONS = {"csv": "text/csv", "md": "text/markdown", "markdown": "text/markdown"}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NAME_SAFE = re.compile(r"[^\w.\- ()\[\]]+")
_ZIP_ENTRIES = 10_000
# Plain text keeps one of these extensions in its name, so code reads as code
# once downloaded; it is still stored and served as text/plain, a download.
CODE_EXTENSIONS = frozenset(
    {
        "py", "ipynb", "js", "mjs", "cjs", "ts", "tsx", "jsx", "vue", "svelte", "html", "htm", "css", "scss",
        "sass", "less", "sql", "sh", "bash", "ps1", "bat", "yaml", "yml", "toml", "ini", "cfg", "conf", "xml",
        "svg", "env", "txt", "log", "rst", "go", "rs", "java", "kt", "c", "h", "cpp", "hpp", "cs", "rb", "php",
        "swift", "dart", "lua", "r", "jl", "tf", "proto", "graphql", "dockerfile", "gitignore", "editorconfig",
        "lock", "properties", "gradle", "makefile", "mk",
    }
)


class FileRejected(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FileKind:
    content_type: str
    extension: str


def _office_or_zip(data: bytes) -> FileKind:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, ValueError):
        raise FileRejected("file_type_unsupported") from None
    if len(names) > _ZIP_ENTRIES:
        raise FileRejected("file_type_unsupported")
    if "[Content_Types].xml" in names:
        for prefix, (content_type, extension) in _OFFICE.items():
            if any(name.startswith(prefix) for name in names):
                return FileKind(content_type, extension)
    return FileKind("application/zip", "zip")


def _text(data: bytes, extension: str) -> FileKind:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise FileRejected("file_type_unsupported") from None
    if _CONTROL.search(text):
        raise FileRejected("file_type_unsupported")
    if extension == "json":
        try:
            json.loads(text)
        except ValueError:
            raise FileRejected("file_type_unsupported") from None
        return FileKind("application/json", "json")
    # Text is text whatever it is called; the name only picks how it is offered.
    content_type = _TEXT_EXTENSIONS.get(extension, "text/plain")
    return FileKind(content_type, {"text/csv": "csv", "text/markdown": "md"}.get(content_type, "txt"))


def sniff(data: bytes, name: str) -> FileKind:
    """The file's type from its bytes; raises FileRejected."""
    if not data:
        raise FileRejected("file_empty")
    if len(data) > MAX_FILE_BYTES:
        raise FileRejected("file_too_large")
    if data.startswith(b"%PDF-"):
        return FileKind("application/pdf", "pdf")
    if data.startswith(b"PK\x03\x04"):
        return _office_or_zip(data)
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _text(data, extension)


def display_name(name: str, kind: FileKind) -> str:
    """A plain file name: no directories or control characters, at most 255
    characters, ending in the extension of what the file really is, or, for
    plain text, its own code extension."""
    base = unicodedata.normalize("NFC", name.replace("\\", "/").rsplit("/", 1)[-1])
    stem, dot, own = base.rpartition(".")
    if not dot:
        stem, own = base, ""
    extension = kind.extension
    if kind.content_type == "text/plain" and own.lower() in CODE_EXTENSIONS:
        extension = own.lower()
    stem = _NAME_SAFE.sub("_", _CONTROL.sub("", stem)).strip(" ._") or "file"
    return f"{stem[: 250 - len(extension)]}.{extension}"


def file_key(org_id: UUID, conversation_id: UUID, operation_id: UUID, kind: FileKind) -> str:
    return f"conversation-images/{org_id}/{conversation_id}/files/{operation_id}.{kind.extension}"
