# Interactive questions and web coverage

Test presence, an isolated unit pass and a full live wrapper pass are different
claims. Release reports identify the candidate and actual runtime/provider tested.
Private incident data and historical recovery journals are not public release assets.

## Boundaries

| Scenario | Evidence source | Limit |
|---|---|---|
| Ordinary durable questions, options/free text, reload/two tabs and turn lock | Questions browser spec and short wrapper | Deterministic LLM, real UI/API/Temporal/runner |
| Answer while exact worker is stopped, consume after restart | Restart wrapper | Separate exact-owned worker lifecycle |
| Natural 3600-second deadline and renewal/idle protection | Hour wrapper | Short/test-clock checks do not substitute |
| Lost durable SSE body/wake-up and reconnection | Client tests and browser fault helper | Permanent auth denial stops retries |
| Kubernetes question lifecycle | Fresh questions k3d wrapper | Real Helm Ingress/UID cleanup; not every hour/restart combination |
| Search/read/citations/source ownership/history | Controlled/public web browser specs | Provider availability differs from controlled fixture behavior |
| SSRF/rebinding/private IPv4 and IPv6 TCP80/443 | Compose and independent K8s network controls | Reachable positive controls mandatory |
| Multi-agent questions, sibling progress, citations and shared budgets | Interactive multi-agent browser scenarios | Exact consume/final and no internal payload exposure |
| Cancel/revoke during HTTP, deadlines and next turn | DB tests plus controlled fault browser scenarios | No late source/final persistence |
| Retry semantics across boundaries | Service/DB/browser tests | 429/nonretryable4xx stay nonretryable; transient attempts bounded |
| Long real-provider dialogue with MCP/questions/web | Opt-in long-dialog spec | Explicit paid provider and separate flags; complete REST/reload history |

## Commands

```bash
QUESTION_ACCEPTANCE_KIND=short ./smoke-test-user-questions.sh
QUESTION_ACCEPTANCE_KIND=restart ./smoke-test-user-questions.sh
QUESTION_ACCEPTANCE_KIND=hour ./smoke-test-user-questions.sh
./smoke-test-user-questions-k8s.sh
WEB_ACCEPTANCE_EXCLUSIVE=1 WEB_ACCEPTANCE_KIND=controlled ./smoke-test-web-tools.sh
WEB_ACCEPTANCE_EXCLUSIVE=1 WEB_ACCEPTANCE_KIND=public ./smoke-test-web-tools.sh
WEB_ACCEPTANCE_EXCLUSIVE=1 ./smoke-test-web-tools-k8s.sh
```

Run sequentially with other organization-mutating/browser acceptance. Provide
matching application/web/search secrets before mutation. Trace/video/screenshots/
storage state are disabled; the dedicated web harness sanitizes matcher diagnostics.

Kubernetes browser traffic uses Helm Ingress through loopback k3d NodePort30080.
Workload/CNI controls stay dual-stack; disabling the system service load balancer
is not permission to drop IPv6 tests. REST-only recovery forwards exist solely for
compatible established recovery paths, not browser SSE transport.

## Recovery and honest status

PASS requires the complete scenario, exact Temporal/resource cleanup, permission
restoration, verify-clean, teardown and infrastructure verification. A partial
browser pass or cleanup-only recovery does not close a gate. Preserve journals/
runtime when exact closure is unavailable. Never change JWT validation, time,
SSRF denies or cleanup ownership to turn a failed run green.

An external search `web_unavailable` is blocked provider coverage until stable
prerequisites return. A mixed real-provider long-dialog gate requires actual
credentials/model; deterministic acceptance does not imply paid-provider coverage.
Report the 26-turn/history/reload combinations explicitly rather than extrapolating
from a shorter conversation or a run with questions/web disabled.
