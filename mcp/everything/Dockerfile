# Reference MCP server for testing the platform's MCP skill integration.
# The package is baked in at build time: the previous stock node:22-slim +
# `npx -y` setup re-downloaded it from the npm registry on every container
# start, i.e. required internet at runtime (breaks air-gapped deployments —
# see docs/AIR_GAPPED.md).
FROM node:22-slim

RUN npm install -g @modelcontextprotocol/server-everything@2026.7.4 \
    && npm cache clean --force

ENV PORT=3001
EXPOSE 3001

CMD ["mcp-server-everything", "sse"]
