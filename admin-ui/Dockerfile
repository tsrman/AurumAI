FROM node:26-alpine AS builder

# Apply base-image security patches: node:22-alpine is rebuilt less often
# than Alpine publishes system-package security fixes.
RUN apk upgrade --no-cache

WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1

COPY package*.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM node:26-alpine AS runner

ARG AURUMAI_SOURCE_URL=""
ARG AURUMAI_REVISION=""
LABEL org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.source="${AURUMAI_SOURCE_URL}" \
      org.opencontainers.image.revision="${AURUMAI_REVISION}"


# Apply base-image security patches: node:22-alpine is rebuilt less often
# than Alpine publishes system-package security fixes.
RUN apk upgrade --no-cache

WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Read these server-side in runtime-config.server.ts. Avoid NEXT_PUBLIC_
# so next build does not inline them; layout.tsx injects the actual values
# into window.__RUNTIME_CONFIG__ on every request. Override them through
# runtime environment/ConfigMap, not build arguments.
# RUNTIME_APP_URL links back to the business-ui chat application.
ENV RUNTIME_API_URL=http://localhost:8001/v1 \
    RUNTIME_KEYCLOAK_URL=http://localhost:8080 \
    RUNTIME_KEYCLOAK_REALM=aurum-ai \
    RUNTIME_KEYCLOAK_CLIENT_ID=admin-ui \
    RUNTIME_APP_URL=http://localhost:3000

# Next.js standalone build.
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

EXPOSE 3000

# node:22-alpine provides the unprivileged node user; the standalone server
# only reads its files and does not need root privileges.
USER node

# Use the Compose healthcheck path so standalone/Kubernetes container
# launches retain the same liveness check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"

CMD ["node", "server.js"]
