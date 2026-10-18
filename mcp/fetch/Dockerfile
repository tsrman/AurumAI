# mcp-server-fetch behind mcp-proxy (the stdio server exposed over SSE).
# The package is baked in at build time: the previous compose entrypoint ran
# `pip install mcp-server-fetch` on every container start, i.e. required
# internet at runtime (breaks air-gapped deployments — see docs/AIR_GAPPED.md).
# Note the tool itself fetches arbitrary URLs by design, so inside an
# air-gapped perimeter it is only useful for internal resources.
FROM ghcr.io/sparfenyuk/mcp-proxy:v0.12.0

# mcp-server-fetch 2026.7.10 imports McpError, which was removed/renamed in
# mcp 2.x. Keep the compatible 1.x API explicit so a fresh build cannot
# resolve to an image that installs successfully and crashes at startup.
RUN pip install --no-cache-dir \
    mcp==1.27.2 \
    mcp-server-fetch==2026.7.10

EXPOSE 8080

ENTRYPOINT ["mcp-proxy", "--port=8080", "--host=0.0.0.0", "--pass-environment", "mcp-server-fetch"]
