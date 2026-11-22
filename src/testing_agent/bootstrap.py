from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from langgraph.graph.state import CompiledStateGraph

from testing_agent.agents.executor import create_executor
from testing_agent.agents.planner import create_planner
from testing_agent.agents.verifier import create_verifier
from testing_agent.integrations.llm.factory import create_model
from testing_agent.integrations.mcp.client import open_mcp_tools
from testing_agent.orchestration.graph import create_graph
from testing_agent.orchestration.policies import RunBudget
from testing_agent.persistence.checkpoints import open_checkpointer
from testing_agent.settings import load_settings
from testing_agent.tools.application import ApplicationScope
from testing_agent.tools.sandbox import SandboxUnavailable

# Ensamblaje de los agentes y los recursos de una investigación.
#Preparamos el grafo y mantenemos sus recursos abiertos durante el proceso.
#Cada investication debe abrir su propio contexto para disponer de una sesion MCP independiente.
#El grafo se utiliza dentro del contexto, ya sea con ainvoke o astream, proporcionando el id de ejecucion mediante configurable.thread_id
@asynccontextmanager
async def open_application(
    server_args: Sequence[str],
    *,
    working_directory: str | Path,
    settings_path: str | Path = "config/application.json",
    checkpoint_path: str | Path = "var/checkpoints/checkpoints.sqlite",
    execution_context: dict | None = None,
    application_scope: ApplicationScope | None = None,
    on_budget: Callable[[RunBudget], None] | None = None,
) -> AsyncGenerator[CompiledStateGraph, None]:
    """Build the agent graph and keep its resources open while it is in use.

    This context manager loads the application settings, creates the Planner,
    Executor, and Verifier models, opens the SQLite checkpointer and MCP
    session, and yields the compiled LangGraph graph.

    Relative ``settings_path`` and ``checkpoint_path`` values are resolved from
    ``working_directory``. Credential paths are relative to the settings
    file unless supplied as absolute paths. ``server_args`` are used to
    start the MCP server and must be supplied by the application, not by the
    model or a tool call.

    Open a separate context for each investigation so each one has its own MCP
    session. Invoke or stream the graph only while this context is open, using
    ``ainvoke`` or ``astream``. Pass the investigation's run ID as
    ``configurable.thread_id`` so its checkpoint state is associated with that
    run.

    Args:
        server_args: Arguments passed to the Python interpreter to start the
            MCP server.
        working_directory: Existing directory used as the base for relative
            configuration and checkpoint paths, and as the MCP subprocess'
            working directory.
        settings_path: Path to the application settings file. Defaults to
            ``config/application.json``.
        checkpoint_path: Path to the SQLite checkpoint database. Defaults to
            ``var/checkpoints/checkpoints.sqlite``.
        on_budget: Optional callback that receives the run's budget as soon as
            it exists, so the caller can request a stop through it.

    Yields:
        The compiled graph, ready to invoke or stream within the context.

    Raises:
        NotADirectoryError: If ``working_directory`` does not exist or is not
            a directory. Configuration, model, checkpointer, and MCP startup
            errors propagate to the caller.

    On context exit, the MCP session and subprocess and the SQLite connection
    are closed, including when an exception propagates.
    """
    
    directory = Path(working_directory).resolve()

    if not directory.is_dir():
        raise NotADirectoryError(directory)

    configuration_path = (directory / settings_path).resolve()
    database_path = (directory / checkpoint_path).resolve()

    settings = load_settings(configuration_path)
    execution = settings["execution"]
    limits = RunBudget(execution["max_tool_calls"], execution["timeout_seconds"])

    if on_budget is not None:
        on_budget(limits)

    planner = create_planner(create_model("planner", settings), asynchronous=True)
    verifier = create_verifier(create_model("verifier", settings), asynchronous=True)
    executor_model = create_model("executor", settings)
    context = execution_context if execution_context is not None else {}

    async with open_checkpointer(database_path) as checkpointer:
        async with AsyncExitStack() as stack:
            try:
                tools = await stack.enter_async_context(open_mcp_tools(
                    server_args, working_directory=directory, execution_context=context,
                ))

            except Exception as error:
                raise SandboxUnavailable("MCP did not establish a verified target session.") from error

            executor = create_executor(executor_model, tools, budget=limits, application_scope=application_scope)

            # instanciamos el grafo con los agentes y el checkpointer para que pueda invocarse
            graph = create_graph(
                planner,
                executor,
                verifier,
                max_iterations=settings["execution"]["max_iterations"],
                checkpointer=checkpointer,
                budget=limits,
            )

            yield graph
