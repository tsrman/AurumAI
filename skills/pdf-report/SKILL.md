---
name: pdf-report
description: Generates a simple one-page PDF report from a title and body text, then verifies the title actually appears in the rendered PDF using OCR. To use, run "python scripts/make_report.py \"<title>\" \"<body>\"" and then "python scripts/verify_report.py \"<title>\"".
---

# PDF report skill

This skill has two steps, run in order — do not skip verification:

1. `python scripts/make_report.py "<title>" "<body text>"` — creates
   `report.pdf` in the current directory using reportlab.
2. `python scripts/verify_report.py "<title>"` — rasterizes the first page
   of `report.pdf` to an image (pdf2image) and runs OCR (pytesseract) on it
   to confirm the title text is actually present in the rendered PDF, not
   just in the source arguments. Prints `VERIFIED: <title>` if found, or
   `NOT VERIFIED` otherwise.

Report both the fact that the PDF was created and the verification result.
