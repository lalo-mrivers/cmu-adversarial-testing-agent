import json
from pathlib import Path
from typing import Any


def load_settings(path: str | Path = "config/application.json") -> dict[str, Any]:
    """Load agent profiles and resolve credential paths relative to the settings file."""
    path = Path(path).resolve()

    with path.open(encoding="utf-8-sig") as file:
        settings = json.load(file)

    for profile in settings["llm"].values():
        profile["credentials_file"] = str((path.parent / profile["credentials_file"]).resolve())

    for agent in ("planner", "executor", "verifier"):
        profile = settings["agents"][agent]

        if profile not in settings["llm"]:
            raise ValueError(
                f"El agente '{agent}' referencia el perfil inexistente '{profile}'."
            )

    max_iterations = settings["execution"]["max_iterations"]

    if type(max_iterations) is not int or max_iterations < 1:
        raise ValueError("execution.max_iterations debe ser un entero positivo.")

    execution = settings["execution"]
    execution.setdefault("max_tool_calls", 10)
    execution.setdefault("timeout_seconds", 120)

    return settings
