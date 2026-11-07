# Network enforcement

Where AurumAI keeps untrusted code and optional features from reaching what they must
not, how that is enforced on Compose and on Kubernetes, and how to check it after you
change something. The details of each feature are in the pages linked below; this page
is the map an operator needs.

## The rule

A feature that reaches outside the platform gets exactly one controlled way out, and
the rule is enforced by the network, not only by application checks. If a check fails,
fix the cause. **Never open outbound access to make a failing check pass**, and never
count an unavailable positive control (a probe that could not even reach the allowed
target) as a passed deny test.

## What is enforced where

| Boundary | Compose | Kubernetes (Helm) | Configure |
|---|---|---|---|
| Runners (conversation code) | Their own bridge network (`RUNNER_DOCKER_NETWORK`); the model gateway is reached through the backend's proxy | NetworkPolicies in the runner namespace: DNS, backend `8000` and listed destinations | `RUNNER_DOCKER_NETWORK`; chart values |
| Skill sandboxes | No network by default | Sandbox namespace policy: `none` denies, `egress` permits bounded public IPv4 and DNS, not private or metadata ranges | Skill settings; chart values |
| Web search and read | `web-tools` behind `web-tools-egress` and `searxng-egress` guards (nftables) on separate networks; profile `web` | Policies for the web namespace | `WEB_ENABLED`, `WEB_EGRESS_FORBIDDEN_CIDRS` |
| Hermes sandboxes | One internal network per sandbox with no NAT and no DNS; `hermes-egress` is the only way out; `hermes-egress-guard` (nftables) closes the rest | `hermes-egress` Deployment; NetworkPolicies are the packet filter | `HERMES_EGRESS_MAX_MODE`, `HERMES_SANDBOX_POOL`, `HERMES_EGRESS_PACKAGE_HOSTS` |
| Background identity worker | `identity-egress` guard allows the platform and the gateway only; profile `maintenance` | Chart policies | `LLM_GATEWAY_URL` |
| Archive worker | `data-egress` guard allows PostgreSQL, Temporal, object storage and the RAG archive endpoint; profile `archive` | Chart policies | `APP_ARCHIVE_*` endpoints |
| Browser-facing ports | Bound to `PUBLIC_BIND_ADDRESS` (loopback by default); publish through the proxy overlay | Ingress | `PUBLIC_BIND_ADDRESS`, `PUBLIC_DOMAIN` |

The guards are containers with `NET_ADMIN` and nothing else (`cap_drop: ALL`,
`no-new-privileges`): they write the packet filter for their network and hold no
application data.

## Always-closed ranges

Private, loopback, link-local and platform ranges are refused **after name resolution**,
and a connection goes to the address that was checked. `WEB_EGRESS_FORBIDDEN_CIDRS`
adds ranges of your own (your VPN, a metadata service, an internal subnet) to the
application, the guards and the Helm policies at once. Do not remove the required
ranges. The one exception is an exact `host:port` in
`HERMES_EGRESS_INTERNAL_PACKAGE_HOSTS`, meant for an internal package mirror.

## Hermes: three caps that stack

1. `HERMES_EGRESS_MAX_MODE` (`none`, `packages`, `public`) is the operator's ceiling for
   the whole deployment: the backend, the proxy and the guard's public ports all follow it.
2. The organization's **egress mode** (Admin → Models) chooses within it.
3. An agent can lower the mode further for its own sessions; it can never raise it.

`public` also needs the person's `web.access` capability. Every decision is a JSON line in
the `hermes-egress` log: session, turn, host, port, mode, outcome and bytes.

## Subnets

Compose networks use fixed ranges (runner `172.28.0.0/24`, skills `172.29.0.0/24`,
RAG `172.30.0.0/24`) and, for Hermes, a pool from which every sandbox gets its own
`/28` (`HERMES_SANDBOX_POOL`, default `172.31.0.0/16`). They must not overlap your
corporate or VPN ranges, or each other. Set the Hermes pool **before** the first sandbox
is created, and pick a pool that is free on the host.

## After you change something

Order matters; a guard that started before its target changed holds the old picture.

1. Recreate the services you changed.
2. If the **backend** was recreated, restart `aurum-ai-hermes-egress-guard` and
   `aurum-ai-hermes-egress-1`. The guard resolves the backend's address once at start;
   until then Hermes turns fail with `hermes_run_failed`.
3. Check `/ready`, then try the feature's allowed path and one refused path (a private
   address, a host outside the mode).
4. For a change to ranges, modes or networks, run the matching acceptance on a
   disposable project, not on the live stack:

| Change | Acceptance |
|---|---|
| Web boundary | `WEB_ACCEPTANCE_EXCLUSIVE=1 WEB_ACCEPTANCE_KIND=controlled ./smoke-test-web-tools.sh` (`public` for the internet path) |
| Hermes egress | `./smoke-test-hermes-egress.sh` (needs internet; not at the same time as the sandbox acceptance, because there is one guard) |
| Kubernetes policy | `./smoke-test-web-tools-k8s.sh`, `./smoke-test-hermes-k8s-egress.sh` and the other `k8s` scripts in [testing](../TESTING.md) |

## Kubernetes

The CNI must **enforce** NetworkPolicy; a cluster that accepts the objects and ignores
them is the most common silent failure. k3s's built-in controller and Calico were
checked; verify any other controller (Cilium, a cloud CNI) with the acceptance above
before you rely on it: that a sandbox Pod reaches only the guard, that no DNS escapes,
and that the guard's exclusions hold. The Pod API has no portable PID limit, so a
RuntimeClass or node setting must provide it. Production values must list explicit
external CIDRs and ports; the development private-range defaults are not an allowance.
See the [chart guide](https://github.com/tsrman/AurumAI/blob/main/charts/aurum-ai/README.md).

## Related

- [Security model](../reference/security.md#network)
- [Hermes sandbox egress](../CONFIGURATION.md#hermes-sandbox-egress) and
  [Hermes on Kubernetes](../CONFIGURATION.md#hermes-on-kubernetes)
- [Questions and web](../CONFIGURATION.md#questions-and-web)
- [Air-gapped deployment](../AIR_GAPPED.md)
