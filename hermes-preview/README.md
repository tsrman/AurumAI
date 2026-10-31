# hermes-preview

Previews of the apps Hermes Agent runs in its sandbox, each at an origin of its
own (`HERMES_PREVIEW_ORIGIN`, a template with `{preview}`). A browser enters with
a one-time ticket the backend issued, keeps the preview's grant in a host-only
cookie, and every new connection is checked with the backend. Requests reach the
app through skill-executor's stream into the sandbox; upgraded connections are
spliced and rechecked. See docs/HERMES_AGENT.md.

    uv sync && uv run pytest
