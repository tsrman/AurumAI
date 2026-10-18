#!/usr/bin/env bash
# Seed two MCP and two code skills for deliberate manual development use.
# This is not a test: acceptance creates and removes its own skills.
# See cleanup in smoke-test-mcp.sh and smoke-test-code-skill.sh
# plus the browser fixture hooks; after cleanup there may be no skills
# left to select in the UI.
#
# Repeatable development setup removes same-named demo skills first,
# then creates the intended fixtures again.
#
# Requires a running trusted development stack and the optional profiles
# used by these example skills:
#   docker compose --profile mcp up -d mcp-everything mcp-fetch
#   docker compose --profile skills-exec up -d skill-executor
#   docker compose --profile build build skill-sandbox
#
# Register MCP skills through the admin API on8001, like admin-ui,
# rather than inserting raw database rows. Smoke uses direct fixture SQL
# only for its intentionally non-administrative test actor.
#
# Tool-call compliance depends on the configured model.
# Some models emit the requested tool protocol consistently, while others
# answer simple tasks without using a tool; test the chosen provider.
set -euo pipefail

ADMIN_BASE_URL="${ADMIN_BASE_URL:-http://localhost:8001}"
KEYCLOAK_URL="${KEYCLOAK_URL:-http://localhost:8080}"
REALM="${REALM:-aurum-ai}"
BUNDLE_DIR="$(mktemp -d)"
trap 'rm -rf "$BUNDLE_DIR"' EXIT

log() { echo "[seed] $*"; }

token=$(curl -sf -X POST \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password" -d "client_id=admin-ui" \
  -d "username=admin" -d "password=admin" \
  "$KEYCLOAK_URL/realms/$REALM/protocol/openid-connect/token" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
log "admin-ui token received"

# Remove same-named demo fixtures before repeated manual seeding.
existing=$(curl -sf "$ADMIN_BASE_URL/v1/skills/" -H "Authorization: Bearer $token")
echo "$existing" | python3 -c "
import sys, json
names = {'Calculator (MCP)', 'Web page fetch (MCP)', 'CSV analysis', 'PDF report'}
for s in json.load(sys.stdin)['data']:
    if s['name'] in names:
        print(s['id'])
" | while read -r id; do
  [ -n "$id" ] && curl -sf -X DELETE "$ADMIN_BASE_URL/v1/skills/$id" -H "Authorization: Bearer $token" >/dev/null && log "removed previous skill $id"
done

create_mcp_skill() {
  local name="$1" description="$2" endpoint="$3" activity="$4" schema="$5"
  curl -sf -X POST "$ADMIN_BASE_URL/v1/skills/" \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    -d "{\"name\":\"$name\",\"description\":\"$description\",\"type\":\"mcp\",
         \"mcpTransport\":\"sse\",\"mcpEndpoint\":\"$endpoint\",
         \"activityName\":\"$activity\",\"inputSchema\":$schema,\"permissions\":[]}" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])"
}

log "Registering MCP skills..."
id=$(create_mcp_skill "Calculator (MCP)" \
  "Adds two numbers. Takes the arguments a and b." \
  "http://mcp-everything:3001/sse" "get-sum" \
  '{"type":"object","properties":{"a":{"type":"number"},"b":{"type":"number"}},"required":["a","b"]}')
log "  Calculator (MCP): $id"

id=$(create_mcp_skill "Web page fetch (MCP)" \
  "Fetches the URL content and returns it as text. Takes the url argument." \
  "http://mcp-fetch:8080/sse" "fetch" \
  '{"type":"object","properties":{"url":{"type":"string"}},"required":["url"]}')
log "  Web page fetch (MCP): $id"

# Code-skill development fixtures.
zip_bundle() {
  local src="$1" out="$2"
  python3 -c "
import zipfile, os, sys
base, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, 'w') as zf:
    for root, _, files in os.walk(base):
        for f in files:
            full = os.path.join(root, f)
            zf.write(full, os.path.relpath(full, base))
" "$src" "$out"
}

upload_code_skill() {
  local name="$1" description="$2" zip_path="$3"
  curl -sf -X POST "$ADMIN_BASE_URL/v1/skills/code" \
    -H "Authorization: Bearer $token" \
    -F "name=$name" -F "description=$description" \
    -F "bundle=@$zip_path;type=application/zip" \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['id'])"
}

# CSV statistics with pandas: write source data, then analyze in another round.
csv_dir="$BUNDLE_DIR/csv-src"
mkdir -p "$csv_dir/scripts"
cat > "$csv_dir/SKILL.md" <<'EOF'
---
name: csv-analyzer
description: Computes CSV statistics with pandas, including sum, mean, minimum and maximum for numeric columns. Use for statistics, aggregation or table/CSV analysis.
---

# CSV analysis

The `scripts/analyze.py` script takes a CSV file path and prints statistics
for all numeric columns.

Procedure:

1. Save the user data to a file, for example:
   `python3 -c "open('data.csv','w').write('''<CSV rows>''')"`
2. Run the analysis: `python3 scripts/analyze.py data.csv`
3. Read the output and explain the results in plain language.

If the user provides no data, create a small demonstration dataset
and clearly identify it as a demonstration.
EOF
cat > "$csv_dir/scripts/analyze.py" <<'EOF'
import sys

import pandas as pd

path = sys.argv[1] if len(sys.argv) > 1 else "data.csv"
df = pd.read_csv(path)

print(f"Rows: {len(df)}, columns: {len(df.columns)}")
print(f"Columns: {', '.join(df.columns)}")

numeric = df.select_dtypes("number")
if numeric.empty:
    print("No numeric columns.")
else:
    print("\nNumeric column statistics:")
    for column in numeric.columns:
        series = numeric[column]
        print(
            f"  {column}: sum={series.sum():.2f} mean={series.mean():.2f} "
            f"min={series.min():.2f} max={series.max():.2f}"
        )
EOF
zip_bundle "$csv_dir" "$BUNDLE_DIR/csv.zip"
id=$(upload_code_skill "CSV analysis" "Computes CSV statistics with pandas in the sandbox" "$BUNDLE_DIR/csv.zip")
log "  CSV analysis (code): $id"

# PDF generation through reportlab exercises binary/file outputs.
pdf_dir="$BUNDLE_DIR/pdf-src"
mkdir -p "$pdf_dir/scripts"
cat > "$pdf_dir/SKILL.md" <<'EOF'
---
name: pdf-report
description: Creates a PDF with a title and text using reportlab and reports the file size. Use for PDF, report or document requests.
---

# PDF report

The `scripts/make_pdf.py` script creates a PDF:

    python3 scripts/make_pdf.py "<title>" "<paragraph text>"

It prints the path and size of the generated file. Run it with the content
requested by the user, then confirm the document was created and report
its size.
EOF
cat > "$pdf_dir/scripts/make_pdf.py" <<'EOF'
import os
import sys

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

title = sys.argv[1] if len(sys.argv) > 1 else "Report"
body = sys.argv[2] if len(sys.argv) > 2 else "No report text was provided."

path = "report.pdf"
styles = getSampleStyleSheet()
doc = SimpleDocTemplate(path, pagesize=A4)
doc.build(
    [
        Paragraph(title, styles["Title"]),
        Spacer(1, 12),
        Paragraph(body, styles["BodyText"]),
    ]
)

print(f"PDF created: {path} ({os.path.getsize(path)} bytes)")
EOF
zip_bundle "$pdf_dir" "$BUNDLE_DIR/pdf.zip"
id=$(upload_code_skill "PDF report" "Builds a PDF with reportlab in the sandbox" "$BUNDLE_DIR/pdf.zip")
log "  PDF report (code): $id"

log "Done. Current skill list:"
curl -sf "$ADMIN_BASE_URL/v1/skills/" -H "Authorization: Bearer $token" \
  | python3 -c "
import sys, json
for s in json.load(sys.stdin)['data']:
    print(f\"  [{s['type']}] {s['name']} — {s['id']}\")
"
