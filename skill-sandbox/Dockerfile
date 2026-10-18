# Fixed base image for code skill sandbox sessions (see skill-executor/).
# Deliberately no build tools and no pip in the runtime path an uploaded
# bundle could reach — only what's baked in here by a real commit+rebuild.
FROM python:3.12-slim
COPY LICENSE /app/LICENSE

ARG AURUMAI_SOURCE_URL=""
ARG AURUMAI_REVISION=""
LABEL org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.source="${AURUMAI_SOURCE_URL}" \
      org.opencontainers.image.revision="${AURUMAI_REVISION}"


# Apply base-image security patches: python:3.12-slim is rebuilt
# less often than Debian publishes fixes; scans found high-severity
# issues in system packages such as util-linux and libexpat.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

# System binaries required by the pinned Python libraries below (see
# requirements.txt for which skill needs which):
#   poppler-utils     -> pdf2image (pdftoppm/pdftocairo)
#   tesseract-ocr      -> pytesseract (OCR), English by default
#   tesseract-ocr-rus  -> pytesseract with lang="rus"/"eng+rus" (Cyrillic) —
#                         reportlab's base-14 fonts (Helvetica etc.) have no
#                         Cyrillic glyphs either, so a Cyrillic PDF also needs
#                         a real font: see fonts-dejavu-core below.
#   fonts-dejavu-core  -> DejaVuSans(-Bold).ttf at
#                         /usr/share/fonts/truetype/dejavu/ — register it via
#                         reportlab's pdfmetrics.registerFont(TTFont(...))
#                         instead of "Helvetica"/"Helvetica-Bold" whenever a
#                         skill's PDF text may be non-Latin.
#   libreoffice-*      -> docx/pptx/xlsx skills' office/soffice.py (headless
#                         conversion for rendering/thumbnails/recalculation);
#                         scoped to writer/calc/impress, skipping draw/base/math
#                         to keep the image smaller.
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-rus \
        fonts-dejavu-core \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

RUN useradd -u 10001 -m -s /usr/sbin/nologin skilluser
RUN mkdir -p /skill && chown skilluser:skilluser /skill

USER skilluser
WORKDIR /skill

CMD ["sleep", "infinity"]
