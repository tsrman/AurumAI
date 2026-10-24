# Installation starter tools

The installation entry point starts the ordinary Compose platform, the existing
isolated code-skill executor, and the pinned reference MCP utilities server. It
registers four operator-owned global skills:

| Skill | Type | Function | Input |
| --- | --- | --- | --- |
| CSV statistics | Code, offline | Count, missing values, sum, mean, minimum and maximum for numeric columns | A UTF-8 CSV file supplied by the user |
| JSON validation and formatting | Code, offline | Validate JSON and print sorted, indented JSON | A UTF-8 JSON file supplied by the user |
| Calculator (MCP) | SSE MCP | Add two numbers with `get-sum` | `a` and `b`, both numbers |
| Text round-trip (MCP) | SSE MCP | Return supplied text with `echo` | `message`, a string |

The code bundles use only Python's standard library and have `network_policy=none`.
CSV input is limited to 1 MiB, 10,000 data rows and 100 columns. Empty numeric
cells are excluded from statistics. Mixed text/numeric columns are reported as
nonnumeric; duplicate or blank headers, malformed rows and nonfinite numbers fail
validation. JSON input is limited to 1 MiB; duplicate keys and nonfinite numbers
fail validation. Formatting preserves values and does not overwrite the input.

These tools do not fetch URLs, access external accounts, or enable `web.access`.
The wrapper starts only `mcp-everything` from the MCP profile; it does not start
`mcp-fetch`. The reference server package is pinned in
[its Dockerfile](../mcp/everything/Dockerfile). The calculator and echo definitions
select only those two tools from that server.

## Install with Compose

Configure the ordinary stack environment first, following the
[README](../README.md) and [configuration guide](CONFIGURATION.md). For a fresh
local development stack, the existing `scripts/bootstrap-dev-env.sh` command can
create the initial environment. On an existing stack, retain its current `.env`,
`APP_SECRET_KEY`, database credentials and storage credentials. This installer
does not generate or rotate secrets. Model/provider configuration remains an
operator task.

Review the commands without running Docker or writing anything:

```bash
./install-platform.sh
```

Start the platform and install the global catalog:

```bash
./install-platform.sh --apply
```

To give existing organizations initial read access to these four tools only,
name each target explicitly:

```bash
./install-platform.sh --apply --org-id <organization-uuid> --org-id <another-organization-uuid>
```

The wrapper builds the conversation-runner and skill-sandbox images, starts
ordinary services with a bounded Compose wait, then starts `mcp-everything` and
`skill-executor` before applying the catalog through the backend container.
It uses the repository's ordinary `docker-compose.yml` and clears inherited
`COMPOSE_PROFILES` for this invocation so optional archive, maintenance, RAG or
fetch services are not added. It preserves the existing Compose project and
secret environment. Run this entry point separately from acceptance wrappers
that mutate organizations.

If the platform already runs and its backend image includes this source,
review or apply just the catalog through the operator CLI:

```bash
docker compose exec -T backend python -m src.cli.install_starter_tools
docker compose exec -T backend python -m src.cli.install_starter_tools --apply --org-id <organization-uuid>
```

For a backend development environment, use `uv run python -m
src.cli.install_starter_tools` from `business-backend/`. The CLI dry-run is offline:
it prints deterministic IDs and bundle checksums without opening a database or
object-storage connection. Apply uses `APP_DATABASE_URL` and the backend's normal
`APP_S3_*` settings. It rejects superuser, RLS-bypass and schema-owner connections;
use the runtime role. It never reads a migration URL or escalates a database role.

## Idempotence and authorization

Each catalog entry has a stable UUIDv5 identity and a reserved `workflow_type`
ownership marker, `aurum-starter-v1:<catalog-key>`. The marker is metadata for
code/MCP entries and does not select a Temporal workflow. Keep it when editing
operator-owned definitions. A same-ID resource without the marker, a tenant-owned
resource or a conversation-owned resource is a collision and stops installation.
Same-named resources with different identities remain untouched.

Reruns preserve all fields of existing owned definitions, including names,
instructions, schemas, bundle pointers, endpoints and credentials. They do not
upgrade bundles or move existing endpoints. New bundle ZIPs use sorted source
paths, fixed timestamps and SHA-256 content-addressed storage keys. Object writes
are create-only; an existing object must contain identical bytes. The operation
runs in one database transaction with a catalog advisory lock. A failed or
uncertain storage write may leave a content-addressed object but cannot publish a
partially registered catalog or grants. Retain such objects for reconciliation;
no destructive cleanup is attempted.

The optional `--org-id` targets receive only organization-level skill read grants,
through the existing RLS-scoped provisioning helper. No model grants,
capabilities, defaults, groups or memberships change. New organizations use the
existing global-skill provisioning contract. Existing organizations receive no
automatic backfill unless explicitly targeted.

First provisioning of each tool to a named organization records a deterministic
`starter_provision` event in `authorization_audit_events` in the same transaction.
The event's fixed actor UUID identifies this operator installer; its payload holds
only safe permission metadata. These receipts are scoped by organization and
remain independent of access grants. A later run skips a receipt already present,
so an administrator's deleted grant stays deleted. Preserve these receipts as
authorization history, including across backup and restore; do not remove them as
fixture metadata. They are not conversation payload rows and are not rewritten by
the conversation archive lifecycle. If a tool needs access again after revocation,
use the ordinary explicit resource-grant administration flow.

An alternate credential-free SSE endpoint can be supplied for initial creation:

```bash
./install-platform.sh --mcp-endpoint https://mcp.example.org/sse
```

Append `--apply` to execute. User information, URL query parameters and fragments
are rejected. The endpoint must be reachable from tool-worker. Existing tools keep
their saved endpoint and token even when this flag changes. An operator changing
an endpoint must review the definition and provide replacement credentials
explicitly through the established administration mechanism.

## Use the tools

After installation, grant the relevant skill resources to the organization/user
as needed, configure an authorized model and select the skills on an agent. A
newly installed catalog alone does not grant model access or chat capabilities.
Examples to send to an agent configured with the corresponding tool:

```text
Use CSV statistics on this data and explain the missing value:
team,sales
A,10
B,20
C,
```

Expected numeric result: count 2, missing 1, sum 30, mean 15, minimum 10, maximum 20.

```text
Validate and format this JSON with the JSON tool: {"b":2,"a":1}
```

Expected result: valid, indented JSON with keys `a`, `b` and unchanged values.
Duplicate keys such as `{"a":1,"a":2}` produce a validation error.

```text
Use Calculator (MCP) to add 17 and 25. Report the tool result.
Use Text round-trip (MCP) with message "starter-check". Report the returned text.
```

Expected tool outputs include 42 and `starter-check`, respectively. Actual model
selection and tool-call behavior depend on the configured provider. A model
answering from memory does not establish that a tool executed.

## Validation coverage

Executable backend tests cover bundle execution and input limits, deterministic
catalog bytes, dry-run behavior, runtime-role RLS, tenant isolation, foreign ID
collisions, concurrent installation, scoped provisioning receipts and preserving
operator edits/revoked grants. Wrapper tests execute the shell with an inert Docker
recorder to verify selected commands and retained environment. See
[testing](TESTING.md) for service lint/type/test commands and isolated database
requirements. Starter tests live in `business-backend/tests/starter_tools/`.

These checks do not prove a real MCP SSE exchange, sandbox container execution,
provider inference, Kubernetes CNI behavior or production installation. Those
require separate operator acceptance against the intended deployment. The
installer itself does not invoke those acceptance scenarios or mutate existing
organization configuration to make them pass.
