"""Exact membership recovery for the disposable lifecycle acceptance realm only."""

import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler
from uuid import UUID

STATE = Path("/tmp/phaseb-lifecycle-acceptance/keycloak-membership.json")
PROJECT = "phaseb-lifecycle-test"
BASE = "http://127.0.0.1:18080"
_token = None
_http = build_opener(ProxyHandler({}))


def owned_keycloak():
    result = subprocess.run(["docker", "inspect", PROJECT + "-keycloak-1"],
                            check=True, capture_output=True, text=True)
    info = json.loads(result.stdout)[0]
    labels = info["Config"]["Labels"]
    if (labels.get("com.docker.compose.project") != PROJECT
            or labels.get("com.docker.compose.service") != "keycloak"
            or info.get("State", {}).get("Running") is not True
            or info["NetworkSettings"]["Ports"].get("8080/tcp") != [
                {"HostIp": "127.0.0.1", "HostPort": "18080"}]):
        raise ValueError("owned_keycloak_required")
    return info["Id"]


def request(method, path):
    global _token
    if _token is None:
        body = urlencode({"grant_type": "password", "client_id": "admin-cli",
                          "username": "admin", "password": "admin"}).encode()
        with _http.open(Request(BASE + "/realms/master/protocol/openid-connect/token", data=body), timeout=15) as response:
            _token = json.load(response)["access_token"]
    req = Request(BASE + "/admin/realms/aurum-ai" + path, method=method,
                  headers={"Authorization": "Bearer " + _token})
    with _http.open(req, timeout=15) as response:
        body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            raise ValueError("bounded_response_required")
        return json.loads(body) if body else None


def prepare():
    container = owned_keycloak()
    if STATE.exists():
        raise ValueError("membership_recovery_required")
    users = request("GET", "/users?username=user&exact=true")
    groups = [g for g in request("GET", "/groups") if g["name"] == "analysts"]
    if len(users) != 1 or users[0]["username"] != "user" or len(groups) != 1:
        raise ValueError("owned_fixture_identity_required")
    user, group = str(UUID(users[0]["id"])), str(UUID(groups[0]["id"]))
    membership = request("GET", f"/users/{user}/groups")
    state = {"version": 2, "phase": "prepared", "keycloak_container_id": container, "user_id": user,
             "group_id": group, "was_member": any(g["id"] == group for g in membership)}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump(state, output)
        output.flush()
        os.fsync(output.fileno())
    return state


def scope():
    state = json.loads(STATE.read_text())
    if state["version"] != 2 or type(state["was_member"]) is not bool:
        raise ValueError("invalid_membership_journal")
    if owned_keycloak() != state["keycloak_container_id"]:
        raise ValueError("established_keycloak_mismatch")
    user, group = str(UUID(state["user_id"])), str(UUID(state["group_id"]))
    actual_user = request("GET", f"/users/{user}")
    actual_group = request("GET", f"/groups/{group}")
    if (actual_user["id"] != user or actual_user["username"] != "user"
            or actual_group["id"] != group or actual_group["name"] != "analysts"):
        raise ValueError("established_membership_identity_mismatch")
    return state, f"/users/{user}/groups/{group}"


def save_phase(state, phase):
    state["phase"] = phase
    temporary = STATE.with_suffix(".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as output:
        json.dump(state, output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, STATE)
    directory = os.open(STATE.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def present(state):
    actual = request("GET", f"/users/{state['user_id']}/groups")
    return any(g["id"] == state["group_id"] for g in actual)


def revoke():
    state, path = scope()
    if state["phase"] != "prepared":
        raise ValueError("membership_operation_unresolved")
    if not state["was_member"]:
        return
    save_phase(state, "delete_pending")
    request("DELETE", path)
    save_phase(state, "delete_done")


def recover():
    state, path = scope()
    phase = state["phase"]
    if phase == "delete_pending":
        # Original presence cannot prove a delayed DELETE has finished.
        if present(state):
            raise ValueError("membership_delete_unresolved")
        save_phase(state, "delete_done")
        phase = "delete_done"
    if phase == "delete_done":
        save_phase(state, "restore_pending")
        try:
            request("PUT", path)
        except Exception:
            pass
        phase = "restore_pending"
    if phase not in {"prepared", "restore_pending"}:
        raise ValueError("membership_phase_unresolved")
    # A pending PUT is never repeated. Presence after observed deletion proves
    # its effect in this exclusive fixture; absence preserves the intent.
    if present(state) != state["was_member"]:
        raise ValueError("membership_restore_unresolved")
    STATE.unlink()


if __name__ == "__main__":
    import fcntl

    STATE.parent.mkdir(parents=True, exist_ok=True)
    lock = os.open(STATE.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if sys.argv[1:] == ["prepare"]:
            print(json.dumps(prepare()))
        elif sys.argv[1:] == ["revoke"]:
            revoke()
        elif sys.argv[1:] == ["recover"]:
            recover()
        else:
            raise ValueError("command_required")
    except Exception:
        raise SystemExit("Owned membership operation unresolved; preserve journal and run data-lifecycle-membership.py recover") from None
