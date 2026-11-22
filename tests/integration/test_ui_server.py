import json
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.integration.test_service import local_settings

ROOT = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))

        return sock.getsockname()[1]


def lan_address() -> str | None:
    """An address of this machine that is not loopback, if it has one."""

    try:
        address = socket.gethostbyname(socket.gethostname())

    except OSError:
        return None

    return None if address.startswith("127.") else address


def reachable(address: str, port: int) -> bool:
    try:
        with socket.create_connection((address, port), timeout=2):
            return True

    except OSError:
        return False


def snapshot(port: int, page: str, **query: str) -> str:
    """Everything the server rendered for a page ("" is the default page), as one string."""
    response = httpx.post(
        f"http://127.0.0.1:{port}/_stcore/agent/interact", timeout=60,
        json={**({"page": page} if page else {}), "query_params": {key: [value] for key, value in query.items()}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready", body
    text = json.dumps(body, ensure_ascii=False)

    return re.sub(r"\\\\([!-/:-@\[-`{-~])", r"\1", text)  # Markdown escapes read as the character.


def wait_for_health(port: int, log: Path) -> None:
    for _ in range(200):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/_stcore/health", timeout=2).text == "ok":
                return

        except httpx.TransportError:
            pass

        time.sleep(0.2)

    pytest.fail("The Streamlit server did not start.\n" + log.read_text(encoding="utf-8", errors="replace")[-3000:])


def stop(process: subprocess.Popen) -> None:
    """Stop a server and, on Windows, the child process the `testing-agent-app` command starts."""

    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)

    else:
        process.terminate()

    process.wait(timeout=30)


def test_real_streamlit_server_runs_an_investigation_and_serves_its_report(tmp_path):
    settings = local_settings(tmp_path)
    state_file = tmp_path / "state.json"
    log_path = tmp_path / "server.log"
    port = free_port()
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.integration.ui_server", "--port", str(port), "--state-file", str(state_file),
         "--working-directory", str(ROOT), "--settings", str(settings), "--runs-path", str(tmp_path / "runs"),
         "--checkpoint-path", str(tmp_path / "checkpoints.sqlite"), "--systems-path", str(tmp_path / "systems")],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
    )

    try:
        wait_for_health(port, log_path)

        for _ in range(300):
            if state_file.exists():
                break

            time.sleep(0.2)

        else:
            pytest.fail("The investigation did not start.\n" + log_path.read_text(encoding="utf-8", errors="replace")[-3000:])

        state = json.loads(state_file.read_text(encoding="utf-8"))

        for _ in range(300):
            page = snapshot(port, "run", id=state["run_id"])

            if "Verdict: No violation observed within the tested scope" in page:
                break

            time.sleep(0.5)

        else:
            pytest.fail("The report never showed the verdict.\n" + page[:1500])

        assert "REQ-PRODUCT-001" in page and "HTTP 200" in page and "Download report" in page

        home = snapshot(port, "")  # The default page tells the story and counts what exists.
        assert "Does your application really follow its business rules?" in home and "Recent investigations" in home
        systems = snapshot(port, "systems")
        assert "Inventory & Reservations" in systems and "1 investigations" in systems
        assert "Test this endpoint" in snapshot(port, "system", id=state["system_id"])

        if lan := lan_address():
            assert not reachable(lan, port), "The server must listen on loopback only."

    finally:
        stop(process)
        log.close()


def test_web_command_listens_on_loopback_only(tmp_path):
    settings = local_settings(tmp_path)
    log_path = tmp_path / "web.log"
    port = free_port()
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-m", "testing_agent_app", "--working-directory", str(ROOT), "--port", str(port),
         "--settings", str(settings), "--runs-path", str(tmp_path / "runs"),
         "--checkpoint-path", str(tmp_path / "checkpoints.sqlite"), "--systems-path", str(tmp_path / "systems")],
        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
    )

    try:
        wait_for_health(port, log_path)
        assert httpx.get(f"http://127.0.0.1:{port}/", timeout=10).status_code == 200

        if lan := lan_address():
            assert not reachable(lan, port), "The web command must listen on loopback only."

        assert not (tmp_path / "systems").exists()  # Starting the server registers nothing.

    finally:
        stop(process)
        log.close()

    console = log_path.read_text(encoding="utf-8", errors="replace")
    assert "Traceback" not in console and "nvalid" not in console, console  # The theme options were accepted.
