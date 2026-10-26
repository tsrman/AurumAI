# AurumAI Hermes Agent sandbox

The fixed image skill-executor runs for a Hermes Agent session
([design](../docs/HERMES_AGENT.md)). It contains
[Hermes Agent](https://github.com/NousResearch/hermes-agent) (MIT, Nous Research),
run from the release's source tree (its GitHub tag archive, verified by sha256),
with every dependency pinned by hash and installed as a wheel, plus `git`, `curl`
and CA certificates. There are no compilers: packages without a wheel for the platform
cannot be installed by the agent.

skill-executor starts it with no capabilities, no new privileges, a read-only root
filesystem, a tmpfs `/tmp`, CPU, memory and PID limits, a workspace volume at
`/workspace` and an internal network of its own whose only way out is
hermes-egress. Before the container starts it places the platform-generated
config (`/workspace/.hermes/config.yaml`, with the session token and the local API
server key) and the egress proxy URL in the workspace, never in the environment.
`start.sh` waits for them and runs `hermes gateway run`; Hermes's API server
listens on 127.0.0.1 only. `hermes_relay.py` (`hermes-relay`) is the only way
skill-executor talks to it, through `docker exec`.

Upgrading Hermes is a deliberate change. Hermes ships no package (its wheel build
refuses outside Nix), so set `HERMES_VERSION` and `HERMES_SHA256` in the
`Dockerfile` to the new tag and the sha256 of
`https://github.com/NousResearch/hermes-agent/archive/refs/tags/v<version>.tar.gz`,
then regenerate `requirements.txt` from that release's own `uv.lock` with

```bash
./update-requirements.sh
```

and re-run `./smoke-test-hermes-sandbox.sh`, because Hermes's configuration keys
and API server behavior are what the sandbox relies on.
