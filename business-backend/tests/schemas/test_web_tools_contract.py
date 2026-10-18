import json
from pathlib import Path

from src.schemas.web_tools import (
    WEB_TOOLS_CONTRACT_VERSION,
    WebReadRequest,
    WebReadServiceResponse,
    WebSearchRequest,
    WebSearchServiceResponse,
)


def test_backend_owns_an_independent_copy_of_the_versioned_web_service_contract() -> None:
    fixture = json.loads(
        (Path(__file__).parents[3] / "contracts/web-tools-v1.json").read_text()
    )

    assert WEB_TOOLS_CONTRACT_VERSION == fixture["version"] == 1
    assert WebSearchRequest.model_validate(fixture["search"]).model_dump(
        exclude_none=True
    ) == fixture["search"]
    assert WebReadRequest.model_validate(fixture["read"]).model_dump(exclude_none=True) == fixture[
        "read"
    ]
    assert WebSearchServiceResponse.model_validate(fixture["search_response"]).model_dump(
        mode="json"
    ) == fixture["search_response"]
    assert WebReadServiceResponse.model_validate(fixture["read_response"]).model_dump(
        mode="json"
    ) == fixture["read_response"]
