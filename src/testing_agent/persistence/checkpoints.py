from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver


@asynccontextmanager
async def open_checkpointer(path: str | Path) -> AsyncGenerator[AsyncSqliteSaver, None]:
    """ Uses sqlite to persist the state of the graph and the agent.
    Opens the checkpoint store and closes the connection on exit.
    
    Relative paths are resolved from the execution directory.
    The context must remain open while the graph is in use.
    """
    database_path = Path(path).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)

    async with AsyncSqliteSaver.from_conn_string(str(database_path)) as checkpointer:
        await checkpointer.setup()
        yield checkpointer