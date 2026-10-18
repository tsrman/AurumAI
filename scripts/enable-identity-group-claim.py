"""Install the signed empty-groups marker in an existing realm; dry-run by default."""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

CLIENTS = ("business-ui", "admin-ui")
CLAIM = "platform_groups_complete"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Operator credentials must remain at the explicitly selected origin.
        return None


def expected_marker():
    realm = json.loads(
        (Path(__file__).resolve().parents[1] / "keycloak/realm-export.json").read_text()
    )
    client = next(row for row in realm["clients"] if row["clientId"] == CLIENTS[0])
    return next(
        row for row in client["protocolMappers"] if row["config"].get("claim.name") == CLAIM
    )


def requires_marker(mappers, expected):
    membership = [row for row in mappers if row.get("config", {}).get("claim.name") == "groups"]
    if (
        len(membership) != 1
        or membership[0].get("protocolMapper") != "oidc-group-membership-mapper"
        or membership[0]["config"].get("access.token.claim") != "true"
    ):
        raise ValueError("Expected one access-token group membership mapper")
    markers = [row for row in mappers if row.get("config", {}).get("claim.name") == CLAIM]
    if not markers:
        return True
    if (
        len(markers) != 1
        or markers[0].get("protocolMapper") != expected["protocolMapper"]
        or any(
            markers[0].get("config", {}).get(key) != value
            for key, value in expected["config"].items()
        )
    ):
        raise ValueError("Conflicting groups completeness mapper")
    return False


def request(url, method="GET", token=None, payload=None, form=None):
    headers = {"Accept": "application/json"}
    body = None
    if token:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if form is not None:
        body = urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    with build_opener(NoRedirect()).open(
        Request(url, data=body, headers=headers, method=method), timeout=15
    ) as response:
        content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("Oversized Keycloak response")
        return json.loads(content) if content else None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--realm", default="aurum-ai")
    parser.add_argument("--auth-realm", default="master")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    endpoint = urlsplit(args.url)
    if (
        endpoint.scheme not in ("http", "https")
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or endpoint.path not in ("", "/")
    ):
        parser.error("--url must be an exact HTTP(S) origin")
    origin = args.url.rstrip("/")
    try:
        username = os.environ["IDENTITY_KEYCLOAK_ADMIN_USERNAME"]
        password = os.environ["IDENTITY_KEYCLOAK_ADMIN_PASSWORD"]
        token = request(
            origin
            + "/realms/"
            + quote(args.auth_realm, safe="")
            + "/protocol/openid-connect/token",
            "POST",
            form={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": username,
                "password": password,
            },
        )["access_token"]
        base = origin + "/admin/realms/" + quote(args.realm, safe="")
        expected = expected_marker()
        planned = []
        for name in CLIENTS:
            clients = request(base + "/clients?" + urlencode({"clientId": name}), token=token)
            if len(clients) != 1 or clients[0].get("clientId") != name:
                raise ValueError("Expected one client")
            url = base + "/clients/" + quote(clients[0]["id"], safe="") + "/protocol-mappers/models"
            if requires_marker(request(url, token=token), expected):
                planned.append((name, url))
        if args.apply:
            for name, url in planned:
                request(url, "POST", token=token, payload=expected)
                if requires_marker(request(url, token=token), expected):
                    raise ValueError("Marker readback failed")
        print(
            json.dumps(
                {
                    "apply": args.apply,
                    "clients": [name for name, url in planned],
                    "verified": args.apply,
                }
            )
        )
    except Exception:
        raise SystemExit(
            "Keycloak marker setup failed; verify credentials, mapper contract and server readiness. Response content suppressed."
        ) from None


if __name__ == "__main__":
    main()
