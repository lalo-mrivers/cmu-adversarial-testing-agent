from collections.abc import Awaitable, Callable
import asyncio
import inspect
import json
from functools import wraps
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from mcp.server.fastmcp.resources import TextResource
from pydantic import AnyUrl

from testing_agent.tools.assertions import check_response
from testing_agent.tools.sandbox import CONTEXT_URI
from testing_agent.orchestration.policies import RunBudget
from testing_agent.persistence.redaction import redact


def create_mcp_server(
    *,
    requirements_lookup: Callable[..., dict[str, Any]],
    application_request: Callable[..., Awaitable[dict[str, Any]]],
    budget: RunBudget | None = None,
    execution_context: dict | None = None,
) -> FastMCP:
    """Registers the tools (found it more convenient this way). 
    Tools prepared for the same investigation and version.

    The application composition provides the functions created by
    create_requirements_lookup and create_application_request. Their signatures and descriptions are preserved so that MCP can publish their input schemas.
    The owner of the HTTP client must keep it open during the service.

    """
    limits = budget or RunBudget()
    signatures = {name: inspect.signature(function) for name, function in (
        ("request_application", application_request), ("check_response", check_response),
        ("lookup_requirements", requirements_lookup),
    )}

    class InvestigationServer(FastMCP):
        async def call_tool(self, name, arguments):
            reason = limits.charge()

            if reason:
                return {"decision": "blocked", "reason": reason}

            # FastMCP may otherwise discard extra fields while coercing arguments.
            if name not in signatures or arguments.keys() - signatures[name].parameters.keys():
                return {"decision": "blocked", "reason": "Unauthorized tool or argument."}

            return await super().call_tool(name, arguments)

    server = InvestigationServer(name="adversarial-testing-agent", log_level="WARNING")

    if execution_context is not None:
        server.add_resource(TextResource(uri=AnyUrl(CONTEXT_URI), name="session",
                                         text=json.dumps(execution_context), mime_type="application/json"))

    def guarded(function):
        @wraps(function)
        async def invoke(**kwargs):
            if limits.pause or limits.seconds_left <= 0:
                return {"decision": "blocked", "reason": limits.pause or "timeout"}

            try:
                async with asyncio.timeout(limits.seconds_left):
                    result = function(**kwargs)

                    if inspect.isawaitable(result):
                        result = await result

            except (Exception, asyncio.CancelledError):
                if function is application_request:
                    limits.pause = limits.pause or "recovery_required"

                    return {"decision": "unknown", "reason": limits.pause}

                raise

            if limits.pause:
                return {"decision": "unknown", "reason": limits.pause}

            return redact(result)

        return invoke

    server.add_tool(
        guarded(requirements_lookup),
        name="lookup_requirements",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    server.add_tool(
        guarded(application_request),
        name="request_application",
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    server.add_tool(
        guarded(check_response),
        name="check_response",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )

    return server


async def run_mcp_server(server: FastMCP) -> None:
    """Serves an investigation via stdio in the current async loop.
    
    It must run within the context that keeps the HTTP client open.
    stdout is reserved for the MCP protocol; diagnostic messages must be sent to stderr.
    Errors and cancellation propagate to the owner so that its contexts can release resources."""
    await server.run_stdio_async()
