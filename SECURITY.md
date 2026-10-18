# Security policy

## Reporting a vulnerability

Use the repository's **Security → Report a vulnerability** option for a private
report when private vulnerability reporting is enabled. Do not post credentials,
tenant data, exploit payloads, network captures or private recovery journals in a
public issue. If private reporting is unavailable, open an issue requesting that
maintainers enable it, without sensitive reproduction details.

Private reporting must be enabled and verified before publishing the public
repository. This source snapshot does not claim that a hosted reporting channel
already exists. Maintainers can announce an additional verified private contact
channel in this file later.

Include the source revision, affected component, deployment/runtime, impact,
minimal reproduction and whether the issue crosses a tenant or network boundary.
Use synthetic data. Maintainers may request a coordinated disclosure while a fix
and its regression tests are prepared.

## Support scope

The maintained source and releases receive best-effort security review; there is
no guaranteed response SLA. Operators must review deployment-specific images,
model weights, credentials and infrastructure. Installing source does not make a
development Compose configuration suitable for an untrusted production network.

## Deployment boundaries

- Compose grants the trusted backend Docker-socket authority. Limit host access
  and browser exposure accordingly; use the Helm/RBAC deployment for production
  isolation requirements.
- Browser-facing Compose ports bind to loopback by default. Docker-published
  ports bypass host firewalls, so widen `PUBLIC_BIND_ADDRESS` only on a trusted
  network and publish publicly through `docker-compose.proxy.yml` (TLS, Temporal
  UI behind basic auth).
- Replace development accounts and configure real secrets, TLS and intended
  Keycloak redirect origins before production use.
- Preserve FORCE RLS, separate migration/runtime roles, live authorization and
  session-token audience boundaries.
- Keep code execution, MCP and public web opt-ins behind their deployment policies.
  Do not grant arbitrary outbound network access to make a failed test pass.
- Enable content capture/observability deliberately. Prompts and documents can
  contain sensitive data even when credentials are never logged.
- Preserve exact ownership checks and recovery evidence when a workflow/resource
  cleanup cannot be proved. An ambiguous deletion is not a successful cleanup.

See [deployment](charts/aurum-ai/README.md), [testing](docs/TESTING.md) and
[configuration](docs/CONFIGURATION.md) for the concrete supported procedures.
