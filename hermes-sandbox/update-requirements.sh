#!/bin/sh
# Regenerates requirements.txt from Hermes's own uv.lock for the release the
# Dockerfile pins (HERMES_VERSION, HERMES_SHA256): every dependency of the
# mcp extra and of sms (aiohttp, which the API server needs), with hashes.
# Hermes itself is not in it; the image installs its verified source tree.
set -eu
cd "$(dirname "$0")"
version=$(sed -n 's/^ARG HERMES_VERSION=//p' Dockerfile)
sha256=$(sed -n 's/^ARG HERMES_SHA256=//p' Dockerfile)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL -o "$work/hermes.tar.gz" "https://github.com/NousResearch/hermes-agent/archive/refs/tags/v$version.tar.gz"
echo "$sha256  $work/hermes.tar.gz" | sha256sum -c - >/dev/null
tar -xzf "$work/hermes.tar.gz" -C "$work" --strip-components=1 hermes-agent-"$version"/pyproject.toml hermes-agent-"$version"/uv.lock
# The lock's only VCS requirement is Android's psutil, which pip cannot hash.
(cd "$work" && uvx uv@0.13.1 export --frozen --no-dev --extra mcp --extra sms --no-emit-project \
  --format requirements-txt --no-header) | grep -v '^psutil @ git+' > requirements.txt
# Data libraries for charts and files, pinned with hashes for the image's
# Python and resolved within Hermes's own pins; what requirements.txt already
# pins is left out, as both files install in one pip run.
grep -E '^[A-Za-z0-9_.-]+==' requirements.txt | sed -E 's/ *;.*//; s/ *\\$//' > "$work/constraints.txt"
uvx uv@0.13.1 pip compile requirements-data.in -c "$work/constraints.txt" --generate-hashes \
  --python-version 3.14 --python-platform x86_64-manylinux_2_28 --only-binary :all: \
  --no-header --no-annotate -o "$work/data.txt"
python3 - "$work/data.txt" requirements.txt requirements-data.txt <<'PY'
import re
import sys

compiled, base, output = sys.argv[1:]
pinned = {re.split(r"[=\s;\[]", line, maxsplit=1)[0].lower() for line in open(base) if re.match(r"[A-Za-z0-9_.-]+==", line)}
blocks = re.split(r"\n(?=[A-Za-z0-9_.-]+==)", open(compiled).read().strip())
kept = [block for block in blocks if block.split("==", 1)[0].lower() not in pinned]
open(output, "w").write("\n".join(kept) + "\n")
PY
