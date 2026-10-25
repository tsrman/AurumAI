# AurumAI Hermes Agent sandbox

The fixed image skill-executor runs for a Hermes Agent session
([design](../docs/HERMES_AGENT.md)). It contains
[Hermes Agent](https://github.com/NousResearch/hermes-agent) (MIT, Nous Research),
installed from PyPI with every dependency pinned by hash, plus `git`, `curl` and
CA certificates. There are no compilers: packages without a wheel for the platform
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

Upgrading Hermes is a deliberate change: edit `requirements.in`, then regenerate
`requirements.txt` with

```bash
uv pip compile requirements.in --universal --python-version 3.13 --generate-hashes --no-header -o requirements.txt
```

and re-run `./smoke-test-hermes-sandbox.sh`, because Hermes's configuration keys
and API server behavior are what the sandbox relies on.
