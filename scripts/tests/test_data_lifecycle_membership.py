"""Lost Keycloak replies retain exact original membership recovery intent."""

import importlib.util
import json
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace

import pytest


def setup(monkeypatch, tmp_path, initial=True):
    path = Path(__file__).parents[1] / "data-lifecycle-membership.py"
    spec = importlib.util.spec_from_file_location("lifecycle_membership", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATE", tmp_path / "membership.json")
    monkeypatch.setattr(module, "owned_keycloak", lambda: "owned-keycloak")
    user, group = str(uuid4()), str(uuid4())
    state = {"member": initial, "lose_delete": False, "puts": 0}

    def request(method, path):
        if path == "/users?username=user&exact=true":
            return [{"id": user, "username": "user"}]
        if path == "/groups":
            return [{"id": group, "name": "analysts"}]
        if path == f"/users/{user}":
            return {"id": user, "username": "user"}
        if path == f"/groups/{group}":
            return {"id": group, "name": "analysts"}
        if path == f"/users/{user}/groups" and method == "GET":
            return [{"id": group}] if state["member"] else []
        assert path == f"/users/{user}/groups/{group}"
        if method == "DELETE":
            state["member"] = False
            if state["lose_delete"]:
                raise OSError("simulated lost reply")
        elif method == "PUT":
            state["puts"] += 1
            state["member"] = True
        return None

    monkeypatch.setattr(module, "request", request)
    return module, state


def test_committed_delete_with_lost_reply_is_recovered(monkeypatch, tmp_path):
    module, remote = setup(monkeypatch, tmp_path)
    module.prepare()
    remote["lose_delete"] = True
    with pytest.raises(OSError):
        module.revoke()
    assert not remote["member"]
    assert json.loads(module.STATE.read_text())["was_member"] is True
    remote["lose_delete"] = False
    module.recover()
    assert remote["member"] and not module.STATE.exists()


def test_originally_absent_membership_is_never_granted(monkeypatch, tmp_path):
    module, remote = setup(monkeypatch, tmp_path, initial=False)
    module.prepare()
    module.revoke()
    module.recover()
    assert not remote["member"] and remote["puts"] == 0


def test_pending_journal_cannot_be_overwritten(monkeypatch, tmp_path):
    module, _ = setup(monkeypatch, tmp_path)
    module.prepare()
    previous = module.STATE.read_bytes()
    with pytest.raises(ValueError, match="recovery_required"):
        module.prepare()
    assert module.STATE.read_bytes() == previous


def test_replaced_keycloak_blocks_recovery(monkeypatch, tmp_path):
    module, remote = setup(monkeypatch, tmp_path)
    module.prepare()
    monkeypatch.setattr(module, "owned_keycloak", lambda: "replacement")
    with pytest.raises(ValueError, match="established_keycloak_mismatch"):
        module.recover()
    assert module.STATE.exists() and remote["puts"] == 0


def test_unresolved_restore_preserves_journal(monkeypatch, tmp_path):
    module, remote = setup(monkeypatch, tmp_path)
    module.prepare()
    module.revoke()
    original = module.request

    def unavailable(method, path):
        if method == "PUT":
            raise OSError("unavailable")
        return original(method, path)

    monkeypatch.setattr(module, "request", unavailable)
    with pytest.raises(ValueError, match="membership_restore_unresolved"):
        module.recover()
    assert module.STATE.exists() and not remote["member"]


@pytest.mark.parametrize("kind", ["project", "service", "stopped", "binding"])
def test_keycloak_wrong_destination_blocks_before_login(kind, monkeypatch, tmp_path):
    path = Path(__file__).parents[1] / "data-lifecycle-membership.py"
    spec = importlib.util.spec_from_file_location("lifecycle_membership_ownership", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "STATE", tmp_path / "membership.json")
    info = {"Config": {"Labels": {"com.docker.compose.project": module.PROJECT,
                                   "com.docker.compose.service": "keycloak"}},
            "State": {"Running": True}, "NetworkSettings": {"Ports": {
                "8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18080"}]}}}
    if kind in {"project", "service"}:
        info["Config"]["Labels"][f"com.docker.compose.{kind}"] = "foreign"
    elif kind == "stopped":
        info["State"]["Running"] = False
    else:
        info["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"] = "8080"
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps([info])))
    monkeypatch.setattr(module, "request", lambda *a: pytest.fail("Must not contact unowned endpoint"))
    with pytest.raises(ValueError, match="owned_keycloak_required"):
        module.prepare()
    assert not module.STATE.exists()


@pytest.mark.parametrize("pending_method", ["DELETE", "PUT"])
def test_pending_mutation_is_not_repeated_or_overtaken(pending_method, monkeypatch, tmp_path):
    module, remote = setup(monkeypatch, tmp_path)
    module.prepare()
    original = module.request
    attempts = []

    def delayed(method, path):
        if method == pending_method:
            attempts.append(method)
            raise OSError("accepted but pending")
        return original(method, path)

    monkeypatch.setattr(module, "request", delayed)
    if pending_method == "DELETE":
        with pytest.raises(OSError):
            module.revoke()
    else:
        module.revoke()
    for _ in range(2):
        with pytest.raises(ValueError, match="unresolved"):
            module.recover()
        assert module.STATE.exists()
    assert attempts == [pending_method]
    assert remote["puts"] == 0
    # Only now does the one accepted remote request finish.
    remote["member"] = pending_method == "PUT"
    monkeypatch.setattr(module, "request", original)
    module.recover()
    assert remote["member"] and not module.STATE.exists()
