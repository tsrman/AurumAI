"""A just-signalled conversation may have an unsettled child start for a moment."""

import uuid

import pytest

from src.services import execution_closure
from src.services.execution_closure import ClosureBlocked, ExecutionIdentity


class _Verifier:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls = 0

    async def inspect_tree(self, *args: object) -> list[ExecutionIdentity]:
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution_closure, "UNSETTLED_CHILD_DELAY_SECONDS", 0)


async def test_an_unsettled_child_start_is_waited_for() -> None:
    verifier = _Verifier([ClosureBlocked("child_start_unsettled"), ["tree"]])
    tree = await execution_closure._settled_tree(verifier, uuid.uuid4(), "root", "run")  # type: ignore[arg-type]
    assert tree == ["tree"] and verifier.calls == 2


async def test_a_child_start_that_never_settles_still_blocks() -> None:
    verifier = _Verifier([ClosureBlocked("child_start_unsettled")])
    with pytest.raises(ClosureBlocked, match="child_start_unsettled"):
        await execution_closure._settled_tree(verifier, uuid.uuid4(), "root", "run")  # type: ignore[arg-type]
    assert verifier.calls == execution_closure.UNSETTLED_CHILD_ATTEMPTS


async def test_other_closure_blocks_are_not_retried() -> None:
    verifier = _Verifier([ClosureBlocked("child_initiation_missing"), ["tree"]])
    with pytest.raises(ClosureBlocked, match="child_initiation_missing"):
        await execution_closure._settled_tree(verifier, uuid.uuid4(), "root", "run")  # type: ignore[arg-type]
    assert verifier.calls == 1
