"""Controlled upstream behavior, not production HTTP response mocks."""

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest


def fixture():
    path = Path(__file__).parents[1] / "web_tools_fault_fixture.py"
    spec = importlib.util.spec_from_file_location("web_fault_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FaultCases(barrier_seconds=0.1, deadline_seconds=0.01)


def arm(cases, case, mode):
    assert cases.response(f"/control/{case}/arm/{mode}", "POST")[0] == 200


def status(cases, case):
    code, _, body = cases.response(f"/control/{case}/status", "GET")
    assert code == 200
    return json.loads(body)


@pytest.mark.parametrize("mode,codes", [("429", [429, 429]), ("404", [404, 404]), ("503", [503, 200])])
def test_faults_count_only_their_owned_case(mode, codes):
    cases = fixture()
    first, second = str(uuid4()), str(uuid4())
    arm(cases, first, mode)
    arm(cases, second, mode)
    assert [cases.response(f"/fault/{first}", "GET")[0] for _ in range(2)] == codes
    assert status(cases, first)["attempts"] == 2
    assert status(cases, second)["attempts"] == 0
    assert cases.response(f"/control/{first}/arm/{mode}", "POST")[0] == 409


def test_unknown_or_malformed_case_cannot_fetch_or_release():
    cases = fixture()
    for case in ("../page", str(uuid4())):
        assert cases.response(f"/fault/{case}", "GET")[0] == 404
        assert cases.response(f"/control/{case}/release", "POST")[0] == 404


def test_barrier_is_bounded_and_explicit_release_is_observable():
    cases = fixture()
    case = str(uuid4())
    arm(cases, case, "barrier")
    with ThreadPoolExecutor() as pool:
        request = pool.submit(cases.response, f"/fault/{case}", "GET")
        assert cases.response(f"/control/{case}/release", "POST")[0] == 200
        assert request.result(timeout=1)[0] == 200
    assert status(cases, case) == {"attempts": 1, "released": True, "finished": 1}
    other = str(uuid4())
    arm(cases, other, "barrier")
    assert cases.response(f"/fault/{other}", "GET")[0] == 504
    assert status(cases, other)["released"] is False


def test_deadline_case_spans_two_slow_attempts():
    cases = fixture()
    case = str(uuid4())
    arm(cases, case, "deadline")
    assert cases.response(f"/fault/{case}", "GET")[0] == 503
    assert cases.response(f"/fault/{case}", "GET")[0] == 200
    assert status(cases, case)["finished"] == 2
