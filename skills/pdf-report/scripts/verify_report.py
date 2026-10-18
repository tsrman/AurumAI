import sys

import pytesseract
from pdf2image import convert_from_path

title = sys.argv[1] if len(sys.argv) > 1 else ""
images = convert_from_path("report.pdf")
# eng+rus: the sandbox's tesseract-ocr-rus package adds Cyrillic support
# alongside the default English pack — this covers both without needing to
# detect the title's language up front.
text = pytesseract.image_to_string(images[0], lang="eng+rus")

if title in text:
    print(f"VERIFIED: {title}")
else:
    print("NOT VERIFIED")
