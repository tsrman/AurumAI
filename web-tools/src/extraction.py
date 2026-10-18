"""Bounded, non-executing text extraction for already-downloaded documents."""

from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
import codecs
import re

from pypdf import PdfReader


MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_HTML_DEPTH = 256
MAX_TITLE_CHARS = 2000
_BLOCKED_HTML = frozenset({"script", "style", "template", "iframe", "object"})
_VOID_HTML = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_WHITESPACE = re.compile(r"\s+")

# HTML5 lets a document omit these end tags; a start tag in the mapped set
# implicitly closes the open element. Without this the stack never unwinds:
# a page that omits </head> drops its whole body, and a list of bare <li>
# (or <p>) items walks straight into MAX_HTML_DEPTH.
_CLOSES_P = frozenset(
    {
        "address", "article", "aside", "blockquote", "details", "div", "dl",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
        "h4", "h5", "h6", "header", "hgroup", "hr", "main", "menu", "nav", "ol",
        "p", "pre", "section", "table", "ul",
    }
)
_CLOSED_BY: dict[str, frozenset[str]] = {
    "p": _CLOSES_P,
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "option": frozenset({"option", "optgroup"}),
    "optgroup": frozenset({"optgroup"}),
    "thead": frozenset({"tbody", "tfoot"}),
    "tbody": frozenset({"tbody", "tfoot"}),
    "tfoot": frozenset({"tbody"}),
    "tr": frozenset({"tr"}),
    "td": frozenset({"td", "th", "tr"}),
    "th": frozenset({"td", "th", "tr"}),
    "rt": frozenset({"rt", "rp"}),
    "rp": frozenset({"rt", "rp"}),
}
# An open <head> is closed by anything that cannot appear inside it.
_HEAD_CONTENT = frozenset(
    {
        "base", "basefont", "bgsound", "link", "meta", "noscript", "script",
        "style", "template", "title",
    }
)

MAX_CHARSET_SNIFF_BYTES = 4096
_CHARSET_NAME = re.compile(r"[A-Za-z0-9_.:+-]{1,64}")
_META_CHARSET = re.compile(
    rb"""<meta[^>]*?charset\s*=\s*["']?\s*([A-Za-z0-9_.:+-]{1,64})""", re.IGNORECASE
)


def _valid_charset(value: str | None) -> str | None:
    """Accept only a bounded name that is a real bytes-to-text decoder.

    The value is attacker-controlled (a response header, or a <meta> inside the
    downloaded page), and Python ships text-to-text codecs such as rot13 and
    base64 under ordinary-looking names.
    """
    if value is None:
        return None
    name = value.strip().strip("\"'").lower()
    if not _CHARSET_NAME.fullmatch(name):
        return None
    try:
        b"a".decode(name)
    except (LookupError, UnicodeDecodeError, ValueError):
        return None
    return name


def _charset_candidates(body: bytes, declared: str | None) -> Iterator[str]:
    if body.startswith(codecs.BOM_UTF8):
        yield "utf-8-sig"
    if body[:2] in {codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE}:
        yield "utf-16"
    validated = _valid_charset(declared)
    if validated is not None:
        yield validated
    sniffed = _META_CHARSET.search(body[:MAX_CHARSET_SNIFF_BYTES])
    if sniffed is not None:
        meta = _valid_charset(sniffed.group(1).decode("ascii", errors="replace"))
        if meta is not None:
            yield meta
    yield "utf-8"


def _decode_html(body: bytes, declared: str | None) -> str:
    """Honour the declared encoding before assuming UTF-8.

    Dropping the Content-Type charset and decoding everything as UTF-8 turned
    every legacy-encoded page (cp1251 is common on the Russian-language web)
    into a wall of U+FFFD that was then stored as a citable web source.
    """
    for candidate in _charset_candidates(body, declared):
        try:
            return body.decode(candidate)
        except (LookupError, UnicodeDecodeError, ValueError):
            continue
    return body.decode("utf-8", errors="replace")


class ExtractionFailed(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ExtractedText:
    text: str
    title: str | None
    truncated: bool
    page_count: int | None


class _BoundedText:
    def __init__(self, maximum: int) -> None:
        if maximum < 1:
            raise ValueError("maximum must be positive")
        self._maximum = maximum
        self._parts: list[str] = []
        self._length = 0
        self.truncated = False

    def append(self, value: str) -> None:
        normalized = _WHITESPACE.sub(" ", value).strip()
        if not normalized:
            return
        prefix = " " if self._parts and self._length < self._maximum else ""
        remaining = self._maximum - self._length
        candidate = prefix + normalized
        if len(candidate) > remaining:
            self._parts.append(candidate[:remaining])
            self._length = self._maximum
            self.truncated = True
            return
        self._parts.append(candidate)
        self._length += len(candidate)

    def finish(self) -> str:
        return "".join(self._parts).strip()


class _HTMLTextParser(HTMLParser):
    def __init__(self, maximum: int) -> None:
        super().__init__(convert_charrefs=True)
        self.text = _BoundedText(maximum)
        self.title = _BoundedText(MAX_TITLE_CHARS)
        self._stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        lowered = tag.lower()
        if lowered in _VOID_HTML:
            return
        self._close_implied(lowered)
        # Auto-closing keeps the stack honest; it must never become a way to
        # skip this bound, or a blocked tag could be pushed out of view.
        if len(self._stack) >= MAX_HTML_DEPTH:
            raise ExtractionFailed("unsupported_document")
        self._stack.append(lowered)

    def _close_implied(self, opening: str) -> None:
        """Pop elements whose end tag HTML5 allows the document to omit."""
        while self._stack:
            top = self._stack[-1]
            if top == "head":
                if opening in _HEAD_CONTENT:
                    return
            elif opening not in _CLOSED_BY.get(top, frozenset()):
                return
            self._stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered not in self._stack:
            return
        while self._stack:
            if self._stack.pop() == lowered:
                break

    def handle_data(self, data: str) -> None:
        if any(tag in _BLOCKED_HTML for tag in self._stack):
            return
        if "title" in self._stack:
            self.title.append(data)
            return
        if "head" in self._stack:
            return
        self.text.append(data)


def _bounded_body(body: bytes) -> None:
    if not body:
        raise ExtractionFailed("unsupported_document")
    if len(body) > MAX_DOCUMENT_BYTES:
        raise ExtractionFailed("web_too_large")


def extract_html(
    body: bytes, max_chars: int = 50000, charset: str | None = None
) -> ExtractedText:
    _bounded_body(body)
    parser = _HTMLTextParser(max_chars)
    try:
        parser.feed(_decode_html(body, charset))
        parser.close()
    except ExtractionFailed:
        raise
    except Exception as exc:
        raise ExtractionFailed("unsupported_document") from exc
    text = parser.text.finish()
    if not text:
        raise ExtractionFailed("unsupported_document")
    return ExtractedText(
        text=text,
        title=parser.title.finish() or None,
        truncated=parser.text.truncated,
        page_count=None,
    )


def extract_pdf(
    body: bytes,
    max_pages: int = 100,
    max_chars: int = 50000,
) -> ExtractedText:
    _bounded_body(body)
    if max_pages < 1 or max_chars < 1:
        raise ValueError("PDF limits must be positive")
    if not body.startswith(b"%PDF-"):
        raise ExtractionFailed("unsupported_document")
    try:
        reader = PdfReader(BytesIO(body), strict=False)
        if reader.is_encrypted:
            raise ExtractionFailed("unsupported_document")
        total_pages = len(reader.pages)
        processed_pages = min(total_pages, max_pages)
        text = _BoundedText(max_chars)
        has_text = False
        for index in range(processed_pages):
            extracted = reader.pages[index].extract_text() or ""
            if extracted.strip():
                has_text = True
                text.append(f"[Page {index + 1}]\n{extracted}")
        metadata_title = reader.metadata.title if reader.metadata is not None else None
    except ExtractionFailed:
        raise
    except Exception as exc:
        raise ExtractionFailed("unsupported_document") from exc
    if not has_text:
        raise ExtractionFailed("unsupported_document")
    title = str(metadata_title).strip()[:MAX_TITLE_CHARS] if metadata_title else None
    return ExtractedText(
        text=text.finish(),
        title=title or None,
        truncated=total_pages > max_pages or text.truncated,
        page_count=total_pages,
    )
