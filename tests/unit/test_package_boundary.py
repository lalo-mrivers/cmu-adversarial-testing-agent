import ast
from pathlib import Path

LIBRARY = Path(__file__).resolve().parents[2] / "src" / "testing_agent"
APPLICATION_ONLY = ("testing_agent_app", "streamlit")


def test_the_library_does_not_import_the_application_or_streamlit():
    offenders = []

    for path in LIBRARY.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]

            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]

            else:
                continue

            offenders += [f"{path.relative_to(LIBRARY)}: {name}" for name in names
                          if name.split(".")[0] in APPLICATION_ONLY]

    assert not offenders, f"The library imports the application: {offenders}"
