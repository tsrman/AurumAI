import sys

from reportlab.lib.pagesizes import letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

title = sys.argv[1] if len(sys.argv) > 1 else "Report"
body = sys.argv[2] if len(sys.argv) > 2 else ""

# reportlab's base-14 fonts (Helvetica etc.) have no Cyrillic glyphs, so a
# Russian title/body would render as blank boxes — register the sandbox's
# bundled DejaVu Sans instead, which covers Latin and Cyrillic alike.
pdfmetrics.registerFont(TTFont("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
pdfmetrics.registerFont(
    TTFont("DejaVuSans-Bold", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
)

c = canvas.Canvas("report.pdf", pagesize=letter)
c.setFont("DejaVuSans-Bold", 18)
c.drawString(72, 720, title)
c.setFont("DejaVuSans", 12)
c.drawString(72, 690, body)
c.save()
print("Wrote report.pdf")
