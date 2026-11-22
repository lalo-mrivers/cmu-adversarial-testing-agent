import json
import sys
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool, ToolException
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Tool as MCPTool
from pydantic import AnyUrl

from testing_agent.tools.sandbox import CONTEXT_URI, SANDBOX_ORIGIN
from testing_agent.persistence.redaction import redact


_EXPECTED_TOOLS = frozenset({
    "lookup_requirements", "request_application", "check_response",
})


def _adapt_tool(tool: MCPTool, session: ClientSession) -> BaseTool:
    """Preserves the remote schema and the MCP response as evidence."""
    
    async def invoke(**arguments: Any) -> tuple[str, dict[str, Any]]:
        response = await session.call_tool(tool.name, arguments=arguments)
        artifact = redact(response.model_dump(mode="json", by_alias=True, exclude_none=True))
        
        if response.isError:
            # LangChain marcará el ToolMessage como error. Su contenido conserva
            # la respuesta MCP completa, ya que esta vía no devuelve un artifact.
            raise ToolException(json.dumps(artifact, ensure_ascii=False))

        content = response.structuredContent

        if content is None:
            content = artifact["content"]
        
        return json.dumps(redact(content), ensure_ascii=False), artifact

    return StructuredTool(
        name=tool.name,
        description=tool.description or "",
        args_schema=tool.inputSchema,
        coroutine=invoke,
        response_format="content_and_artifact",
        handle_tool_error=True,
        metadata={
            "mcp_annotations": tool.annotations.model_dump(exclude_none=True)
            if tool.annotations is not None else {},
        },
    )


@asynccontextmanager
async def open_mcp_tools(
    server_args: Sequence[str],
    *,
    working_directory: str | Path,
    timeout_seconds: float = 30,
    execution_context: dict | None = None,
) -> AsyncGenerator[list[BaseTool], None]:
    """Start the MCP server and expose its tools for the lifetime of the context.

    The server runs as a Python subprocess and communicates over stdio. This
    function initializes an MCP session, discovers the available tools, and
    requires the server to expose exactly ``lookup_requirements``,
    ``request_application``, and ``check_response``. Each tool is adapted to a
    ``langchain_core.tools.BaseTool`` while preserving its argument schema and
    MCP responses.

    Args:
        server_args: Arguments passed to the Python interpreter to start the
            server, for example ``["path/to/script.py"]`` or
            ``["-m", "package.module"]``. These are not tool arguments. The
            same Python interpreter used by the application is used.
        working_directory: Working directory for the subprocess. It must exist.
        timeout_seconds: Maximum wait time, in seconds, for MCP requests. Must
            be a positive, finite number.

    Yields:
        A list of MCP tools adapted to ``BaseTool``. Invoke them while the
        context is open, for example with ``ainvoke`` or from the Executor.

    Raises:
        ValueError: If the arguments or timeout are invalid, or if the server
            does not expose exactly the expected tools.
        NotADirectoryError: If ``working_directory`` does not exist or is not
            a directory.

    The session and subprocess are closed when the context exits. There are no
    automatic reconnections or retries. Startup, communication, initialization,
    and discovery errors propagate to the caller; if an operation is interrupted,
    its outcome may be uncertain.
    """
    
    if (
        isinstance(server_args, (str, bytes))
        or not server_args
        or any(not isinstance(argument, str) for argument in server_args)
    ):
        raise ValueError("server_args must be a non-empty sequence of strings.")
    
    if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds < float("inf"):
        raise ValueError("timeout_seconds must be positive and finite.")
    
    directory = Path(working_directory).resolve()
    
    if not directory.is_dir():
        raise NotADirectoryError(directory)

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-u", *server_args],
        cwd=str(directory),
    )

    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=timeout_seconds),
        ) as session:
            await session.initialize()

            if execution_context is not None:
                resource = await session.read_resource(AnyUrl(CONTEXT_URI))
                context = json.loads(resource.contents[0].text)

                if (context.get("transport") != "private_asgi" or context.get("mode") != "sandbox"
                    or context.get("account") != "anonymous" or context.get("base_url") != SANDBOX_ORIGIN
                    or context.get("network_restricted") is not True or not context.get("instance_id")):
                    raise RuntimeError("The managed sandbox could not be verified.")

                execution_context.update(context)

            discovered: list[MCPTool] = []
            cursor: str | None = None

            while True:
                page = await session.list_tools(cursor=cursor)
                discovered.extend(page.tools)

                if page.nextCursor is None:
                    break

                if not page.tools or len(discovered) >= len(_EXPECTED_TOOLS):
                    raise ValueError("The server returned an unexpected paginated tool catalog.")

                cursor = page.nextCursor

            names = [tool.name for tool in discovered]

            if len(names) != len(_EXPECTED_TOOLS) or set(names) != _EXPECTED_TOOLS:
                raise ValueError("The server must expose exactly the expected investigation tools.")

            yield [_adapt_tool(tool, session) for tool in discovered]
