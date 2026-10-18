"""Real Keycloak omits an empty group list; a signed marker disambiguates it."""

import json
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def operator_module():
    spec = importlib.util.spec_from_file_location(
        "group_marker", ROOT / "scripts/enable-identity-group-claim.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_operator_does_not_mark_missing_or_conflicting_membership_as_complete():
    import pytest

    module = operator_module()
    with pytest.raises(ValueError):
        module.requires_marker([], module.expected_marker())


def test_operator_marker_is_idempotent_and_rejects_conflict():
    import pytest

    module = operator_module()
    group = {
        "protocolMapper": "oidc-group-membership-mapper",
        "config": {"claim.name": "groups", "access.token.claim": "true"},
    }
    marker = module.expected_marker()
    assert module.requires_marker([group], marker)
    assert not module.requires_marker([group, marker], marker)
    changed = {**marker, "config": {**marker["config"], "claim.value": "false"}}
    with pytest.raises(ValueError):
        module.requires_marker([group, changed], marker)


def test_both_clients_attest_complete_groups_next_to_membership_mapper():
    realm = json.loads((ROOT / "keycloak/realm-export.json").read_text())
    for name in ("business-ui", "admin-ui"):
        client = next(row for row in realm["clients"] if row["clientId"] == name)
        mappers = client["protocolMappers"]
        assert any(
            row["protocolMapper"] == "oidc-group-membership-mapper"
            and row["config"]["claim.name"] == "groups"
            for row in mappers
        )
        marker = next(
            row for row in mappers if row["config"].get("claim.name") == "platform_groups_complete"
        )
        assert marker["protocolMapper"] == "oidc-hardcoded-claim-mapper"
        assert marker["config"]["claim.value"] == "true"
        assert marker["config"]["jsonType.label"] == "boolean"
        assert marker["config"]["access.token.claim"] == "true"


def test_operator_never_forwards_admin_token_through_redirect(monkeypatch):
    import io
    from email.message import Message
    from urllib.error import HTTPError
    import urllib.request
    from urllib.response import addinfourl
    import pytest

    module = operator_module()
    seen = []

    class RedirectTransport(urllib.request.HTTPHandler):
        def http_open(self, request):
            seen.append(request.full_url)
            headers = Message()
            if request.host == "identity.example":
                headers["Location"] = "http://other.example/capture"
                response = addinfourl(io.BytesIO(b""), headers, request.full_url, 302)
                response.msg = "Found"
                return response
            response = addinfourl(io.BytesIO(b"{}"), headers, request.full_url, 200)
            response.msg = "OK"
            return response

    original_builder = urllib.request.build_opener
    monkeypatch.setattr(urllib.request, "_opener", original_builder(RedirectTransport()))
    if hasattr(module, "build_opener"):
        monkeypatch.setattr(
            module,
            "build_opener",
            lambda *handlers: original_builder(*handlers, RedirectTransport()),
        )
    with pytest.raises(HTTPError):
        module.request("http://identity.example/admin", token="synthetic-test-token")
    assert seen == ["http://identity.example/admin"]
