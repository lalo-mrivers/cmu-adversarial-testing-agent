import asyncio
import json
from pathlib import Path

import pytest

from testing_agent.domain.requirements import OperationReference, RequirementCatalog
from testing_agent.integrations.mcp.server import create_mcp_server
from testing_agent.orchestration.policies import RunBudget
from testing_agent.tools.application import create_application_request
from testing_agent.tools.requirements import create_requirements_lookup

CATALOG = Path(__file__).resolve().parents[2] / "specifications/target_app/v1/requirements.json"


class RecordingClient:
    def __init__(self):
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append(kwargs)

        return {"status_code": 200, "body": "{}"}


def call(arguments):
    catalog = RequirementCatalog.model_validate(json.loads(CATALOG.read_text(encoding="utf-8-sig")))
    client = RecordingClient()
    server = create_mcp_server(
        requirements_lookup=create_requirements_lookup(CATALOG, "v1"),
        application_request=create_application_request(
            client, catalog, [OperationReference(method="POST", path="/reservations")]),
        budget=RunBudget(5, 30),
    )
    base = {"specification_version": "v1", "method": "POST", "path": "/reservations"}
    asyncio.run(server.call_tool("request_application", {**base, **arguments}))

    return client.calls[0]


@pytest.mark.parametrize("text", [
    '{"product_id": "product-001", "quantity": 1, "quantity": 2}',
    '{"product_id": "product-001", "quantity": 1}',
    "null",
    "[1, 2]",
    '{"product_id": "product-001", "quantity": ',
])
def test_text_reaches_the_http_client_exactly_as_sent(text):
    assert call({"body": text})["body"] == text


@pytest.mark.parametrize("arguments", [{}, {"body": None}])
def test_an_omitted_or_null_body_is_still_accepted(arguments):
    assert call(arguments)["body"] is None


def test_json_body_still_carries_structured_json():
    assert call({"json_body": {"product_id": "product-001", "quantity": 1}})["json_body"] == {
        "product_id": "product-001", "quantity": 1}


def test_the_published_schema_still_describes_body_as_nullable_text():
    catalog = RequirementCatalog.model_validate(json.loads(CATALOG.read_text(encoding="utf-8-sig")))
    server = create_mcp_server(
        requirements_lookup=create_requirements_lookup(CATALOG, "v1"),
        application_request=create_application_request(
            RecordingClient(), catalog, [OperationReference(method="POST", path="/reservations")]),
    )
    tool = next(item for item in asyncio.run(server.list_tools()) if item.name == "request_application")
    assert tool.inputSchema["properties"]["body"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
