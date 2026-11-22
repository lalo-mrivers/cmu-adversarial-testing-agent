import json
from typing import Any

#from anyio import Path
from pathlib import Path
from langchain.chat_models import init_chat_model
from langchain_core.language_models.chat_models import BaseChatModel


def create_model(agent: str, settings: dict[str, Any]) -> BaseChatModel:
    """Build the assigned model using the credential file resolved by settings."""
    profile_name = settings["agents"][agent]
    profile = settings["llm"][profile_name]

    credentials_path = Path(profile["credentials_file"])

    with credentials_path.open(encoding="utf-8-sig") as file:
        api_key = json.load(file)["api_key"]

    if not api_key:
        raise ValueError(f"credentials_file for agent '{agent}' is empty or missing 'api_key'.")

    return init_chat_model(
        model=profile["model"],
        model_provider=profile["provider"],
        api_key=api_key.strip(),
        **profile.get("options", {}),
    )
