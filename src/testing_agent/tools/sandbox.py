import argparse
import asyncio
import json
import math
import socket
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from testing_agent.integrations.target.http_client import SANDBOX_ORIGIN, TargetHttpClient, open_http_client
from testing_agent.orchestration.policies import RunBudget


CONTEXT_URI = "sandbox://session/context"


class SandboxUnavailable(RuntimeError):
    """The target session could not be started and verified."""


def restrict_target_process(budget: RunBudget) -> None:
    """Install once in the dedicated stdio server, before loading the target."""

    def restrict(event: str, args: tuple) -> None:
        if event in {"socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto",
                     "socket.sendmsg", "subprocess.Popen", "os.system", "os.posix_spawn"}:
            budget.pause = "containment_lost"
            raise PermissionError("Network access and child processes are disabled in the sandbox.")

    sys.addaudithook(restrict)

    try:
        sys.audit("socket.connect", None, None)

    except PermissionError:
        if budget.pause == "containment_lost":
            budget.pause = None

            return

    raise RuntimeError("Sandbox network restriction could not be verified.")


@asynccontextmanager
async def open_managed_target(seed_path: Path) -> AsyncGenerator[tuple[TargetHttpClient, dict], None]:
    """Bind HTTP dispatch directly to one in-memory inventory, without a listener."""
    from sandbox.target_app.src.app import create_app

    application = create_app(seed_path)
    session = SandboxSession(
        instance_id=str(uuid4()), base_url=SANDBOX_ORIGIN,
        initialized_at=datetime.now(timezone.utc).isoformat(),
        initial_state=application.state.inventory.snapshot_state(),
    )
    context = {
        **session.execution_context(), "transport": "private_asgi", "account": "anonymous",
        "dependencies": ["in-memory inventory", "read-only seed"],
    }

    async with open_http_client(SANDBOX_ORIGIN, application=application) as client:
        yield client, context


@dataclass(frozen=True)
class SandboxSession:
    """Identifies a sandbox and its observed state before serving requests."""

    instance_id: str
    base_url: str
    initialized_at: str
    initial_state: dict[str, Any]

    def execution_context(self) -> dict[str, Any]:
        """Returns JSON-compatible execution metadata."""

        return {
            "mode": "sandbox",
            "instance_id": self.instance_id,
            "base_url": self.base_url,
            "initialized_at": self.initialized_at,
            "initial_state": self.initial_state,
            "external_modifications_excluded": False,
        }


async def _stop_process(
    process: asyncio.subprocess.Process,
) -> None:
    """Stops only the process created by this module."""

    if process.returncode is None:
        try:
            process.terminate()

        except ProcessLookupError:
            pass

    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)

    except TimeoutError:
        if process.returncode is None:
            try:
                process.kill()

            except ProcessLookupError:
                pass

        await process.wait()


@asynccontextmanager
async def open_sandbox_session(
    project_directory: str | Path,
    *,
    seed_path: str | Path | None = None,
    startup_timeout_seconds: float = 30.0,
) -> AsyncGenerator[SandboxSession, None]:
    """Starts a new application and yields its base URL.

    Relative paths to the seed file are resolved relative to the project
    directory.

    Each context has its own process, port, and inventory. On exit, that
    process is stopped and its state is discarded.
    """

    if (
        not math.isfinite(startup_timeout_seconds)
        or startup_timeout_seconds <= 0
    ):
        raise ValueError(
            "startup_timeout_seconds must be positive and finite."
        )

    directory = Path(project_directory).resolve()

    if not directory.is_dir():
        raise NotADirectoryError(directory)

    seed = (
        Path(seed_path)
        if seed_path is not None
        else Path("sandbox/target_app/seed/products.json")
    )

    if not seed.is_absolute():
        seed = directory / seed

    seed = seed.resolve()

    if not seed.is_file():
        raise FileNotFoundError(seed)

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-m",
        "testing_agent.tools.sandbox",
        "--seed-file",
        str(seed),
        cwd=str(directory),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,
    )

    try:
        if process.stdout is None:
            raise RuntimeError(
                "The sandbox startup channel is unavailable."
            )

        try:
            line = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=startup_timeout_seconds,
            )

        except TimeoutError as error:
            raise TimeoutError(
                "The sandbox did not become ready within "
                f"{startup_timeout_seconds} seconds."
            ) from error

        if not line:
            raise RuntimeError(
                "The sandbox exited before announcing readiness. "
                "Check its error output."
            )

        try:
            message = json.loads(line)

        except (ValueError, UnicodeError) as error:
            raise RuntimeError(
                "The sandbox returned an invalid startup message."
            ) from error

        if (
            not isinstance(message, dict)
            or set(message) != {
                "event",
                "port",
                "instance_id",
                "initialized_at",
                "initial_state",
            }
            or message["event"] != "ready"
            or type(message["port"]) is not int
            or not 1 <= message["port"] <= 65535
            or not isinstance(message["instance_id"], str)
            or not message["instance_id"]
            or not isinstance(message["initialized_at"], str)
            or not message["initialized_at"]
            or not isinstance(message["initial_state"], dict)
            or set(message["initial_state"]) != {
                "products",
                "reservations",
            }
            or not isinstance(message["initial_state"]["products"], list)
            or message["initial_state"]["reservations"] != []
        ):
            raise RuntimeError(
                "The sandbox returned an unexpected startup message."
            )

        if process.returncode is not None:
            raise RuntimeError(
                "The sandbox exited during startup "
                f"with code {process.returncode}."
            )

        yield SandboxSession(
            instance_id=message["instance_id"],
            base_url=f"http://127.0.0.1:{message['port']}",
            initialized_at=message["initialized_at"],
            initial_state=message["initial_state"],
        )

    finally:
        await _stop_process(process)


@asynccontextmanager
async def open_sandbox(
    project_directory: str | Path,
    *,
    seed_path: str | Path | None = None,
    startup_timeout_seconds: float = 30.0,
) -> AsyncGenerator[str, None]:
    """Preserves the URL-only interface used by the current CLI."""
    
    async with open_sandbox_session(
        project_directory,
        seed_path=seed_path,
        startup_timeout_seconds=startup_timeout_seconds,
    ) as session:
        yield session.base_url


async def _serve_child(seed_path: Path) -> None:
    """Runs the application inside the child process."""
    # stdout is reserved for a single startup notification.
    # Application and server messages go to stderr.
    startup_output = sys.stdout
    sys.stdout = sys.stderr

    import uvicorn

    from sandbox.target_app.src.app import create_app

    application = create_app(seed_path)
    instance_id = str(uuid4())
    initial_state = application.state.inventory.snapshot_state()
    initialized_at = datetime.now(timezone.utc).isoformat()

    with socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM,
    ) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)

        port = listener.getsockname()[1]

        server = uvicorn.Server(
            uvicorn.Config(
                application,
                host="127.0.0.1",
                port=port,
                workers=1,
                reload=False,
                proxy_headers=False,
                forwarded_allow_ips="",
                log_level="info",
            )
        )

        server_task = asyncio.create_task(
            server.serve(sockets=[listener])
        )

        try:
            while not server.started:
                if server_task.done():
                    await server_task
                    raise RuntimeError(
                        "The server stopped before completing startup."
                    )

                await asyncio.sleep(0.05)

            if server_task.done():
                await server_task
                raise RuntimeError(
                    "The server stopped before announcing readiness."
                )

            #print(
            #    json.dumps({"event": "ready", "port": port}),
            #    file=startup_output,
            #    flush=True,
            #)
            print(
                json.dumps({
                    "event": "ready",
                    "port": port,
                    "instance_id": instance_id,
                    "initialized_at": initialized_at,
                    "initial_state": initial_state,
                }),
                file=startup_output,
                flush=True,
            )

            await server_task

        finally:
            server.should_exit = True

            if not server_task.done():
                server_task.cancel()

            await asyncio.gather(
                server_task,
                return_exceptions=True,
            )


def _main() -> None:
    """Internal entry point for the child process."""
    parser = argparse.ArgumentParser(
        description="Child process for the controlled application."
    )
    parser.add_argument(
        "--seed-file",
        type=Path,
        required=True,
    )
    args = parser.parse_args()

    try:
        asyncio.run(_serve_child(args.seed_file))

    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    _main()
