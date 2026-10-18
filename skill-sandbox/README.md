# AurumAI code-skill sandbox

This fixed image executes tenant code only in an ephemeral owned session controlled
by skill-executor. It is not a shared tool-worker process. Preserve round/time/resource
bounds and the selected network policy. Uploaded bundles contain source SKILL.md and
optional scripts; validate ownership/provenance before executing them.

Build with the documented Compose build profile or the configured Kubernetes runtime
image. See [architecture](../docs/ARCHITECTURE.md),
[deployment](../charts/aurum-ai/README.md) and [testing](../docs/TESTING.md).
