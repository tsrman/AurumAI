"""The private acceptance fixture supplies stable vectors, without an external model."""

import importlib.util
import math
from pathlib import Path

import pytest

PATH = Path(__file__).parents[1] / "fixtures" / "data-lifecycle-embeddings.py"


def load_fixture():
    spec = importlib.util.spec_from_file_location("lifecycle_embeddings", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_vectors_are_stable_normalized_and_content_sensitive():
    fixture = load_fixture()
    first = fixture.embed("owned archive document")
    assert len(first) == 1536
    assert first == fixture.embed("owned archive document")
    assert first != fixture.embed("another document")
    assert math.isclose(sum(v * v for v in first), 1, rel_tol=1e-6)
    assert all(math.isfinite(v) for v in first)


@pytest.mark.parametrize("inputs", [None, "one", [], [7], ["x"] * 65])
def test_fixture_rejects_malformed_or_unbounded_batches(inputs):
    with pytest.raises(ValueError):
        load_fixture().response_for({"input": inputs})


def test_fixture_preserves_batch_order():
    fixture = load_fixture()
    response = fixture.response_for({"input": ["alpha", "beta"]})
    assert [row["index"] for row in response["data"]] == [0, 1]
    assert response["data"][0]["embedding"] == fixture.embed("alpha")
    assert response["data"][1]["embedding"] == fixture.embed("beta")
