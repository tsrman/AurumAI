# AurumAI quickstart

This guide starts a trusted local development installation from a clean source
checkout. It uses the mock model first, without paid-provider credentials.

## Prerequisites

Install Docker/Compose v2, Bash and OpenSSL. Reserve ports 3000, 3001, 3100, 8000,
8001, 8080 and 8082, plus loopback infrastructure ports used by Compose. Plan at
least about 8 GB free RAM and 15 GB disk for the base stack; optional RAG models
and build caches can require substantially more.

## 1. Create persistent local secrets

```bash
./scripts/bootstrap-dev-env.sh
```

This creates private ignored `.env` from the tracked example, generates missing
required secrets, and preserves configured values on subsequent runs. It refuses
symlink targets and duplicate secret assignments. Never commit the generated file.
Development identity accounts remain the explicitly documented realm fixtures;
replace them before production use.

## 2. Build the conversation runner

```bash
docker compose --profile build build runner
```

This is required: the backend creates a separate container from the runner image
for each conversation. An ordinary Compose `up` does not build this profile.

## 3. Start and check readiness

```bash
docker compose up -d --build
curl --fail http://localhost:8000/health
curl --fail http://localhost:8000/ready
curl --fail http://localhost:8080/realms/aurum-ai
```

Keycloak and Temporal can take longer to initialize than the APIs. Check service
health and `docker compose logs` for the failing component rather than assuming
that an HTTP health response proves all optional features are ready.

## 4. Log in and send a message

Open `http://localhost:3000`. Log in as `user`/`user` or `admin`/`admin`.
Administration is at `http://localhost:3100` with the `admin` account.
The realm also contains `user2`/`user2` and `admin2`/`admin2` for a second tenant.

Send a short message. With `APP_DEBUG=true` and no gateway credential, the response
comes from the development mock. This still exercises UI, authentication, API,
Temporal and the isolated conversation runner. Open history and reload to check
that the conversation persists.

## 5. Configure optional features

Use [README](README.md), the [deployment guide](docs/guides/deployment.md) and
[configuration](docs/CONFIGURATION.md) for a real gateway, MCP, code skills, RAG,
questions, web, images, Hermes Agent and maintenance workers. Real-model
E2E requires an explicit `E2E_LLM_MODEL`; no personal provider model is preselected.

Browser-facing ports bind to `PUBLIC_BIND_ADDRESS`, which defaults to
`127.0.0.1`. If the browser runs on a different host of a trusted network, set
`PUBLIC_HOST` to an address reachable by that browser and `PUBLIC_BIND_ADDRESS`
to the matching interface (or `0.0.0.0`) before recreating the containers.
Docker-published ports bypass host firewalls such as ufw/firewalld. Runtime
API/Keycloak links are read on each server request, not compiled into the image.

To publish the stack on the internet, use the Traefik overlay with TLS described
in [the README](README.md#publish-through-a-reverse-proxy) instead of widening
`PUBLIC_BIND_ADDRESS`.

## Troubleshooting

**Healthy stack, but the first chat cannot start:** build the runner image first.
Check that the API has the expected Docker socket/network access on this trusted
development host. Kubernetes uses a different runtime/RBAC boundary.

**Member cannot see a model or newly added global skill:** access is default-deny.
New organizations are provisioned on first login. Existing organizations can be
updated deliberately:

```bash
docker compose exec backend python -m src.cli.provision_org --all
docker compose exec backend python -m src.cli.provision_org --all --apply
```

The first command previews; the second applies grants. Do not grant unrelated
capabilities to make a test pass.

**Existing realm does not match current clients/mappers:** initial realm import
runs on a fresh Keycloak database. Use a reviewed realm upgrade for an existing
installation; deleting volumes is not a harmless repair operation.

**Configured evaluator does not run:** set both its enabled flag and a supported
explicit model. Optional evaluation failure does not replace a successful chat
answer with an empty message.

**Optional RAG is not ready:** first model downloads and warming can take minutes.
Use the RAG guide; do not equate an empty search with successful indexing.

## Stop while preserving data

```bash
docker compose down
```

Delete volumes only for an intentionally disposable installation. Existing Redis
volumes are not automatically downgraded to the source release's Redis 7.2 image;
review data-format compatibility before reusing an older installation.
