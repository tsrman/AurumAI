import json
import subprocess
import sys
import uuid

prefix = "phaseb-egress-" + uuid.uuid4().hex[:8]
image = "aurum-ai-maintenance-egress:phaseb"
owned = []
if sys.argv[1:] not in ([], ["--data"]):
    raise SystemExit("Usage: check-identity-egress.py [--data]")
data_mode = sys.argv[1:] == ["--data"]


def run(*args):
    result = subprocess.run(["docker", *args], text=True, capture_output=True, check=True)
    return (result.stdout + (result.stderr if args[0] == "logs" else "")).strip()


listener = """import socket, threading, time
for port in (5432,7233,8000,6379,9000,8111):
 s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(('0.0.0.0',port)); s.listen()
 def serve(s):
  while True:
   c,a=s.accept(); c.close()
 threading.Thread(target=serve,args=(s,),daemon=True).start()
time.sleep(300)
"""
try:
    run("network", "create", "--label", "aurum-ai.task=phaseb-20260913", prefix)
    for suffix in ("peer", "denied"):
        name = prefix + "-" + suffix
        owned.append(
            run(
                "run",
                "-d",
                "--name",
                name,
                "--label",
                "aurum-ai.task=phaseb-20260913",
                "--network",
                prefix,
                "--entrypoint",
                "python3",
                image,
                "-c",
                listener,
            )
        )
    guard = prefix + "-guard"
    peer = prefix + "-peer"
    environment = (
        ["-e", "MAINTENANCE_TCP_ENDPOINTS=" + json.dumps([[peer, 5432], [peer, 7233]]),
         "-e", "MAINTENANCE_HTTP_ENDPOINTS=" + json.dumps([f"http://{peer}:9000", f"http://{peer}:8111"])]
        if data_mode else
        ["-e", "MAINTENANCE_DATABASE_HOST=" + peer, "-e", "MAINTENANCE_TEMPORAL_HOST=" + peer,
         "-e", "LLM_GATEWAY_URL=http://" + peer + ":8000"]
    )
    owned.append(
        run(
            "run",
            "-d",
            "--name",
            guard,
            "--label",
            "aurum-ai.task=phaseb-20260913",
            "--network",
            prefix,
            "--cap-drop=ALL",
            "--cap-add=NET_ADMIN",
            *environment,
            image,
        )
    )
    import time

    for _ in range(20):
        state = json.loads(run("inspect", guard))[0]["State"]
        if state.get("Health", {}).get("Status") == "healthy":
            break
        if not state["Running"]:
            raise RuntimeError(run("logs", guard))
        time.sleep(1)
    else:
        raise RuntimeError("guard not ready: " + run("logs", guard))
    checks = (
        [(peer, 5432, True), (peer, 7233, True), (peer, 9000, True), (peer, 8111, True),
         (peer, 6379, False), (peer, 8000, False), (prefix + "-denied", 9000, False)]
        if data_mode else
        [(peer, 5432, True), (peer, 7233, True), (peer, 8000, True),
         (peer, 6379, False), (prefix + "-denied", 8000, False)]
    )
    probe = "import socket\nchecks=" + repr(checks) + "\n" + """
for host,port,expected in checks:
 try:
  with socket.create_connection((host,port),timeout=1): actual=True
 except OSError: actual=False
 assert actual==expected,(host,port,actual)
print('Exact configured endpoints and forbidden controls: PASS')
"""
    print(
        run(
            "run",
            "--rm",
            "--network",
            "container:" + guard,
            "--cap-drop=ALL",
            "--user",
            "10001:10001",
            "--entrypoint",
            "python3",
            image,
            "-c",
            probe,
        )
    )
finally:
    for container in reversed(owned):
        run("rm", "-f", container)
    run("network", "rm", prefix)
