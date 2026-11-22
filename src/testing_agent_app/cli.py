import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from testing_agent.persistence.redaction import redact


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="testing-agent-app",
        description="Serve the local web interface (loopback only).",
    )
    parser.add_argument("--working-directory", type=Path, default=Path("."))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--settings", type=Path, default=Path("config/application.json"))
    parser.add_argument("--runs-path", type=Path, default=Path("var/runs"))
    parser.add_argument("--checkpoint-path", type=Path, default=Path("var/checkpoints/checkpoints.sqlite"))
    parser.add_argument("--systems-path", type=Path, default=Path("var/systems"))

    return parser


def _serve(args: argparse.Namespace, directory: Path) -> int:
    """Serve the Streamlit interface in a child process, on this machine only.

    There is deliberately no option to change the address. Telemetry is off, the file watcher is
    off (nothing here is developed live) and the investigation runs inside this server process.
    """

    try:
        import streamlit  # noqa: F401

    except ImportError as error:
        raise RuntimeError("The web application needs Streamlit: pip install -e .[app]") from error

    import testing_agent_app.ui
    from testing_agent_app.ui.theme import theme_flags

    app = Path(testing_agent_app.ui.__file__).with_name("app.py")
    command = [
        sys.executable, "-m", "streamlit", "run", str(app),
        "--server.address", "127.0.0.1", "--server.port", str(args.port), "--server.headless", "true",
        "--server.fileWatcherType", "none", "--browser.gatherUsageStats", "false",
        "--client.toolbarMode", "minimal", *theme_flags(), "--",
        "--working-directory", str(directory), "--settings", str(args.settings),
        "--runs-path", str(args.runs_path), "--checkpoint-path", str(args.checkpoint_path),
        "--systems-path", str(args.systems_path),
    ]
    print(f"Web interface: http://127.0.0.1:{args.port}", file=sys.stderr)

    return subprocess.run(command, cwd=directory).returncode


def main(argv: Sequence[str] | None = None) -> int:
    """Validates input and returns the process exit code."""
    args = _build_parser().parse_args(argv)

    try:
        directory = args.working_directory.resolve()

        if not directory.is_dir():
            raise NotADirectoryError(directory)

        return _serve(args, directory)

    except KeyboardInterrupt:
        return 130

    except Exception as error:
        print(f"Error ({type(error).__name__}): {redact(str(error))}", file=sys.stderr)

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
