# Air-gapped deployment

AurumAI can be built/deployed with operator-prepared dependency, image and model
artifacts. A source checkout alone is not an offline artifact bundle. Prepare and
verify the exact inputs on an authorized connected build host first.

## Prepare artifacts

- Preserve Python uv locks and exact distributions, including the target CPU/OS
  variants. RAG/Docling/PyTorch can require substantial disk/model artifacts.
- Preserve npm locks and registry tarballs for both UIs and maintenance checks.
- For Hermes Agent, mirror the hash-pinned distributions of
  `hermes-sandbox/requirements.txt` and `requirements-data.txt`, the Node.js
  release the Dockerfile pins and Debian's packages, or the built
  `aurum-ai-hermes-sandbox` image. npm installs in a sandbox need an npm registry
  mirror among `HERMES_EGRESS_PACKAGE_HOSTS`.
- Mirror pinned base/application/infrastructure images and record actual digests.
- Prepare pgvector/pg_partman5.5.0 prerequisites and current migration chains.
- Obtain embedding/reranker/parser model revisions from their actual sources,
  verify model-card licenses/checksums, and populate the expected caches.
- Include upstream license notices when redistributing packages/images/weights.

Validate signatures/checksums/provenance before transport. Do not substitute private
API credentials or runtime data for artifacts required to build the source.

## Build and configure inside the boundary

Use internal registries/package mirrors with the same locked versions, trusted CA
and explicit offline caches. The UIs use local system-font fallbacks; builds do not
need Google Fonts. Telemetry settings are separate from application observability.

Configure internal PostgreSQL, Redis, Keycloak, Temporal, MinIO and an approved local
LLM gateway. The runner still uses the backend proxy/session boundary. Leave public
web and unsupported outbound MCP/provider features disabled. A DNS-successful URL
is not authorization to cross the network boundary.

Hermes Agent sandboxes reach package indexes only through hermes-egress. Offline,
set `HERMES_EGRESS_MAX_MODE=packages`, point `HERMES_EGRESS_PACKAGE_HOSTS` at the
internal mirror and list its exact `host:port` in
`HERMES_EGRESS_INTERNAL_PACKAGE_HOSTS`, the only way a private address is allowed;
or keep `none`.

Use [Helm](../charts/aurum-ai/README.md) or trusted-development Compose with
explicit endpoints/secrets and appropriate runtime isolation. Production CIDR,
namespace, RBAC and PID enforcement assumptions remain mandatory offline.

## Verify

Exercise clean bootstrap, readiness, both identity audiences, chat persistence,
workflow/tool paths and RAG if included, with actual prepared artifacts. Check that
outbound traffic is limited to intended internal services and that no build tries
to fetch missing dependencies/model weights. Unit tests cannot prove this network
property. Record unavailable optional features as blocked coverage.

Backups, archive receipts and private recovery journals stay within the deployment's
operator boundary. Preserve exact ownership/history checks during recovery; network
isolation does not make an ambiguous remote write safe to purge.

Bifrost2.1.1 cold startup also initializes its pricing store from public
`getbifrost.ai` metadata. The stock image fails before serving HTTP when that
bootstrap URL is unreachable and no existing pricing data is present. Prewarm
and transfer its persistent data or configure an operator-verified offline
pricing source for an air-gapped deployment; mock mode does not need Bifrost.
