#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
image=$(sed -n 's/^IMAGE=//p' "$script_dir/image.lock")

if [ -z "$image" ]; then
  echo "IMAGE is missing from $script_dir/image.lock" >&2
  exit 1
fi

docker run --rm -i \
  --network none \
  --cap-drop ALL \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e SEARXNG_SECRET=integration-only-secret-not-for-runtime \
  -v "$script_dir/settings.yml:/etc/searxng/settings.yml:ro" \
  --entrypoint /usr/local/searxng/.venv/bin/python \
  "$image" - <<'PY'
import searx.search

searx.search.initialize(enable_metrics=False)

from searx.engines import engines
from searx.query import RawTextQuery

expected_engines = {"brave", "duckduckgo", "startpage", "mojeek", "yahoo", "qwant", "wikipedia"}
assert set(engines) == expected_engines, set(engines)

engine = RawTextQuery("topic !ddg", [])
category = RawTextQuery("topic !general", [])
external = RawTextQuery("topic !!g", [])
lucky = RawTextQuery("topic !!", [])
language = RawTextQuery("topic :afrikaans", [])
timeout = RawTextQuery("topic <3", [])

assert engine.specific and [item.name for item in engine.enginerefs] == ["duckduckgo"]
assert category.specific and {item.name for item in category.enginerefs} == expected_engines
assert external.external_bang == "g"
assert lucky.redirect_to_first_result
assert language.languages == ["af"]
assert timeout.timeout_limit == 3.0

print("Pinned SearXNG controls and engine allowlist verified")
PY
