# AurumAI Helm deployment

The chart is named `aurum-ai`. It deploys business/
admin APIs, both UIs, tool-worker and skill-executor. The API creates one conversation
runner Pod per conversation; skill-executor creates a sandbox Pod per code session.
Optional web and maintenance workers add their own scoped resources.

PostgreSQL, Redis, Keycloak, Temporal, MinIO, RAG API and Langfuse are external.
Provide actual `external.*` endpoints and intended network destinations. The chart
never mounts the Docker socket or substitutes privileged Pods for runtime RBAC.

## Prerequisites and isolation

- Kubernetes, Helm3, kubectl and a CNI that actually enforces NetworkPolicy.
- Three **different** application/release, runner and sandbox namespaces. Coincident
  namespaces are rejected. Production namespace ownership/quotas/Pod Security are
  preferably platform-managed (`runtimeNamespaces.create=false`).
- Registry images/pull secrets in every applicable namespace. Helm does not copy
  operator secrets or registry credentials between namespaces.
- Explicit external CIDRs/ports. Development private-range defaults are not a
  production allowance. Runners receive DNS, backend8000 and listed destinations;
  the external LLM gateway is reached through the backend proxy.
- Sandbox `none` denies egress; `egress` permits bounded public IPv4/DNS exclusions,
  not arbitrary private/metadata access. Kubernetes Pod API does not provide a
  portable PID limit; RuntimeClass/CRI/node enforcement is required independently
  of CPU/memory limits.
- PostgreSQL16 with pgvector and pg_partman5.5.0 before migrations. Existing payload
  partition conversion needs backup and a writer-free maintenance window. Disable
  automatic partition retention/drop; verified archive lifecycle owns payload purge.

## Images

| Values | Default source image identity |
|---|---|
| `backend`, `backendAdmin` | `aurum-ai-backend` |
| `frontend` | `aurum-ai-frontend` |
| `adminUi` | `aurum-ai-admin-ui` |
| `toolWorker` | `aurum-ai-tool-worker` |
| `skillExecutor` | `aurum-ai-skill-executor` |
| `runner` | `aurum-ai-conversation-runner` |
| `skillSandbox` | `aurum-ai-skill-sandbox` |

Build all required images from this source and publish only to an authorized
registry. `image.registry`, tag and pullPolicy are defaults; service blocks override.
Permanent workloads use `imagePullSecrets`; dynamic runners/sandboxes use their own
namespace-specific imagePullSecrets. Do not assume release source implies hosted
images exist. OCI license/source/revision arguments accompany actual build provenance.

## Secret ownership

`secrets.create=true` is development convenience. A chart-created runner Secret
requires chart-created runtime namespaces too. When runtime namespaces are externally
owned, their Secret must already exist even if Helm creates the release Secret.

With `secrets.create=false`, prepare these externally:

- Release Secret (`secrets.name`): `APP_SECRET_KEY`, `APP_DATABASE_ROLE_PASSWORD`,
  `APP_MIGRATION_DATABASE_PASSWORD`, `REDIS_PASSWORD`, `APP_S3_SECRET_KEY`,
  `APP_GATEWAY_API_KEY`, `LANGFUSE_SECRET_KEY`.
- Runner Secret (`runtimeSecrets.runnerName`): `REDIS_URL`, `LANGFUSE_SECRET_KEY`.

Sandbox does not receive normal MinIO credentials. Release/runner/sandbox namespaces,
labels and pull secrets must be ready before install; chart RBAC/policies still target
the declared namespaces. Use a secret store, never source-controlled real values.
Maintenance/archive and web features add independent secret/CIDR prerequisites in
their values/validation templates; ordinary APIs must not receive lifecycle access.

## Render, validate and install

Create an operator-owned values file describing images, external endpoints,
namespace/Secret ownership, real CIDRs/ports and browser-visible Ingress hosts.

```bash
helm lint charts/aurum-ai -f values-production.yaml
helm template aurumai charts/aurum-ai -n aurumai -f values-production.yaml | kubeconform -strict -summary
helm upgrade --install aurumai charts/aurum-ai -n aurumai --create-namespace -f values-production.yaml --wait
```

The chart sets Kubernetes runtime and pool size zero. UIs derive browser origins
from configured Ingress unless explicitly overridden. Their `runtimeConfig.sourceCodeUrl`
points to actual deployment source. No UI rebuild is needed for runtime URL changes.
Migrations run through the dedicated hook/role, not once per API replica startup.

## Workflow and feature rollout

Workflow ceiling/parallelism/diagnostic retention values are shared across both APIs.
Immutable snapshots preserve definition versions; live authorization remains required
before preparation, nodes and synthesis. Keep workflow/temperature/question/web/
lifecycle Temporal patch markers during upgrades.

Questions and web admission switches are independently default-off. Deploy compatible
code/migrations first. Web requires isolated namespaces, configured ingress/service
credential, bounded outbound destinations/search and operator enforcement checks.
`backend.webEnabled=true` without the deployed web boundary is rejected.

Hermes Agent (`hermes.enabled=true`, default off) deploys its network boundary: a
dedicated namespace (`hermes.namespace`, Pod Security `restricted`), the `hermes-egress`
guard and Service, a default-deny policy in which a sandbox Pod reaches only the guard
(no DNS, no peer, no platform service), and a Role for skill-executor over Pods,
`pods/exec` and the guard Service. The guard runs the proxy without nftables because the
policies are the packet filter, and needs a CNI that enforces egress policies (k3s's
built-in controller and Calico were checked; `HERMES_K8S_CNI=calico` runs the acceptance
scripts on Calico). `hermes.egress.maxMode` bounds what organizations may
choose; `internalDestinations` must accompany `internalPackageHosts`: a `cidr` for a mirror
outside the cluster, or a `namespaceSelector` with a `podSelector` for one inside it (never
its Service's address: the policy sees the Pod after the Service's translation). Selectors
take `matchLabels` only, may not be empty and may not select the Hermes or the release
namespace. The shared
private-range exclusions are `web.networkPolicy.excluded*Cidrs`. skill-executor
runs the sandboxes as Pods in that namespace (`hermes.sandbox.*`): one replica, and a
`runtimeClassName` for a node PID limit, since Kubernetes has none per Pod. Previews
(`hermes.preview.enabled`) add the preview service, its Secret and a wildcard Ingress rule
for `hermes.preview.host`; use a domain outside the platform's own. `smoke-test-hermes-k8s.sh`
is the acceptance run on a throwaway k3d cluster, `smoke-test-hermes-k8s-egress.sh` checks
the egress modes against the real internet, and `smoke-test-hermes-k8s-nodes.sh` the
behaviour on three nodes (a cold pull, eviction, a PID limit, a lost node).

Identity synchronization starts with `identitySync.enabled=true` and admission false.
Verify signed complete-groups mappers on both clients and worker readiness, then
turn admission on. Roll back API admission first, then stop the worker. Its queue
is `platform-identity-sync`, without Docker socket, inbound ports or unnecessary
service-account token. Destinations cover only required DB/Temporal/bootstrap gateway.

Data workers/private RAG preserve terminal receipts even with new archives disabled.
Use separate lifecycle database/S3/service-secret permissions and scoped destinations;
see [archive operations](../../docs/operations/data-archive.md).

## Acceptance and recovery

The local k3d smoke builds/imports owned images and exercises install/upgrade/chat/
MCP/code/runtime cleanup/RBAC. Questions/web have independent fresh browser/CNI
wrappers. Browser Ingress uses loopback8081→NodePort30080; the test avoids a system
service-load-balancer dependency without reducing dual-stack workload tests.

Fresh network checks require reachable public/DNS/search and private IPv4/IPv6
positive/negative controls, not merely successful YAML admission. Unit/Helm render
cannot stand in for this proof. On failed cleanup/verify-clean or lost established
journal, preserve cluster/release/Temporal namespace/dependencies/kubeconfig/history.
Use exact identities and Pod UID preconditions, never prefix deletion.

[Testing](../../docs/TESTING.md) defines privacy/exclusivity and honest coverage;
[architecture](../../docs/ARCHITECTURE.md) describes the runtime boundaries. Existing
legacy installations need reviewed migration/grant upgrades rather than silently
turning off RBAC/RLS or recreating namespaces to mask lost history.
