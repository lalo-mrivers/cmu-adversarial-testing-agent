import argparse
import json
import threading
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from streamlit.web import bootstrap

from testing_agent_app.ui import common
from tests.integration.test_experiment_flow import GET, ScriptedModel
from tests.integration.test_mvp_evaluation import assess, plan

APP = Path(common.__file__).with_name("app.py")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--state-file", type=Path, required=True)

    for name in ("working-directory", "settings", "runs-path", "checkpoint-path", "systems-path"):
        parser.add_argument(f"--{name}", type=Path, required=True)

    args = parser.parse_args()

    arguments = {"specification_version": "v1", **GET, "path_parameters": {"product_id": "product-001"}}
    call = AIMessage(content="", tool_calls=[{"id": "call-0", "name": "request_application",
                                              "args": arguments}])
    models = {
        "planner": ScriptedModel([plan]),
        "executor": ScriptedModel([call, AIMessage(content="Done.")]),
        "verifier": ScriptedModel([assess]),
    }
    script_args = ["--working-directory", str(args.working_directory), "--settings", str(args.settings),
                   "--runs-path", str(args.runs_path), "--checkpoint-path", str(args.checkpoint_path),
                   "--systems-path", str(args.systems_path)]
    # The same service object the pages will get: the cache is keyed by these strings.
    service = common._shared_service(str(args.working_directory), str(args.settings), str(args.runs_path),
                                     str(args.checkpoint_path), str(args.systems_path))

    def start_investigation() -> None:
        record = service.import_example()
        run_id = service.start(record.system_id, None, [("GET", "/products/{product_id}")], ["REQ-PRODUCT-001"])
        args.state_file.write_text(json.dumps({"system_id": record.system_id, "run_id": run_id}), encoding="utf-8")

    flag_options = {
        "server.port": args.port, "server.address": "127.0.0.1", "server.headless": True,
        "server.fileWatcherType": "none", "browser.gatherUsageStats": False,
        "server.enableAgentApi": True,  # So a test can read pages over HTTP, without a browser.
    }
    bootstrap.load_config_options(flag_options)  # Without this the flags are ignored.

    with patch("testing_agent.bootstrap.create_model", lambda agent, settings: models[agent]):
        threading.Thread(target=start_investigation, daemon=True).start()
        bootstrap.run(str(APP), False, script_args, flag_options)


if __name__ == "__main__":
    main()
