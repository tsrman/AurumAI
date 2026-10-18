"""Read-only guard before mounting Docker into the disposable lifecycle backend."""

import json
import subprocess

NETWORK = "phaseb-lifecycle-test_runner"


def validate_runner(container: dict) -> None:
    labels = container["Config"].get("Labels") or {}
    name = container["Name"].lstrip("/")
    if labels.get("aurum-ai.component") != "conversation-runner" and not name.startswith("runner-"):
        return
    networks = container["NetworkSettings"]["Networks"]
    if set(networks) != {NETWORK}:
        raise ValueError("foreign_runner")


def main() -> None:
    ids = subprocess.check_output(["docker", "ps", "-aq"], text=True).split()
    if ids:
        # Capture full inspection privately: Docker Config.Env can contain credentials.
        containers = json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True))
        for container in containers:
            validate_runner(container)
    print("No foreign runner can be reached by the test backend reaper")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.SubprocessError, KeyError):
        raise SystemExit("Lifecycle runtime preflight failed; inspect ownership before starting") from None
