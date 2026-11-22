import asyncio
import concurrent.futures
import re
import shutil
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from testing_agent.domain.requirements import OperationReference, Requirement
from testing_agent.persistence.review import register_decision
from testing_agent_app.service import reports
from testing_agent.runner import RunConfig, RunControl, loads_json, prepare_request, run_investigation
from testing_agent_app.service.systems import Endpoint, SystemRecord, SystemStore
from testing_agent.settings import load_settings

MAX_OBJECTIVE_CHARS = 4000
MAX_FOCUS_CHARS = 1000
MAX_IMPORT_BYTES = 512_000


class Busy(RuntimeError):
    """Another investigation is still running in this process."""


class _RunLoop:
    """A private event loop in a daemon thread."""

    def __init__(self) -> None:
        self.loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="investigations", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coroutine) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)

        if not self.thread.is_alive():
            self.loop.close()


@dataclass
class Validation:
    """Outcome of checking a request. Nothing here called an LLM or the target."""

    errors: list[str] = field(default_factory=list)
    system: SystemRecord | None = None
    version: str | None = None
    endpoints: list[Endpoint] = field(default_factory=list)  # Under test.
    requirements: list[Requirement] = field(default_factory=list)
    operations: list[OperationReference] = field(default_factory=list)  # Authorized for the run.
    focus: str = ""
    objective: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def build_objective(
    system: SystemRecord, version: str, endpoints: list[Endpoint],
    requirements: list[Requirement], focus: str,
) -> str:
    """The instruction given to the Planner. Fixed template: the same request gives the same text."""
    lines = [
        f"Investigate whether {', '.join(e.key for e in endpoints)} of the system \"{system.name}\" "
        f"complies with the following requirement(s) of specification {version}.",
        f"System purpose: {system.purpose}",
    ]
    lines += [f"Endpoint under test {e.key}: {e.purpose}" for e in endpoints]

    for rule in requirements:
        lines.append(f"Requirement {rule.id}: {rule.description} "
                     f"Expected behavior according to the catalog: {rule.expected_behavior}")

        if rule.preconditions:
            lines.append(f"Preconditions of {rule.id}: " + " ".join(rule.preconditions))

        for exception in rule.exceptions:
            lines.append(f"Exception {exception.id} of {rule.id}: when {exception.condition} "
                         f"then {exception.expected_behavior}")

    if focus:
        lines.append(f"Additional focus provided by the user (not part of the catalog): {focus}")

    lines.append(
        "Plan a minimal sequence of authorized operations that can show whether the application satisfies "
        "the requirement(s), starting from the initial sandbox state. Authorized operations may include "
        "setup steps that are not under test. Base the assessment on recorded tool observations. "
        "If evidence is incomplete, identify what remains inconclusive.")

    return "\n".join(lines)


class InvestigationService:
    def __init__(
        self,
        working_directory: str | Path,
        *,
        settings: Path = Path("config/application.json"),
        runs_path: Path = Path("var/runs"),
        checkpoint_path: Path = Path("var/checkpoints/checkpoints.sqlite"),
        systems_path: Path = Path("var/systems"),
        example_system: Path = Path("config/examples/inventory_system.json"),
        seed_file: Path = Path("sandbox/target_app/seed/products.json"),
    ) -> None:
        self.directory = Path(working_directory).resolve()

        if not self.directory.is_dir():
            raise NotADirectoryError(self.directory)

        self.settings = settings
        self.runs_path = (self.directory / runs_path).resolve()
        self.checkpoint_path = checkpoint_path
        self.seed_file = (self.directory / seed_file).resolve()
        self.systems = SystemStore(self.directory / systems_path)
        self.example_system = example_system
        self._lock = threading.Lock()
        self._loop: _RunLoop | None = None
        self._active: tuple[RunControl, concurrent.futures.Future] | None = None

    def seed_for(self, system: SystemRecord) -> Path:
        """The seed this system runs on: its own file from the seed folder, else the default."""

        if system.target.seed is None:
            return self.seed_file

        return self.seed_file.parent / system.target.seed

    # ---- systems ----------------------------------------------------------------
    def example_files(self) -> list[str]:
        """The system files offered for import: every JSON file next to the example, by project-relative name."""
        folder = self.directory / self.example_system.parent

        return sorted(path.relative_to(self.directory).as_posix() for path in folder.glob("*.json") if path.is_file())

    def import_example(self, name: str | None = None) -> SystemRecord:
        """Register one of the example files (the default one when no name is given), like any import."""
        chosen = name or self.example_system.as_posix()

        if chosen not in self.example_files():
            raise ValueError("That is not one of the example files.")  # The browser never picks a path.

        return self.systems.import_system_file(self.directory / chosen, base=self.directory)

    def import_upload(self, content: bytes, filename: str) -> SystemRecord:
        """Register a system from an uploaded JSON file, which must carry everything it needs."""

        if len(content) > MAX_IMPORT_BYTES:
            raise ValueError(f"The file is larger than {MAX_IMPORT_BYTES // 1000} KB.")

        try:
            data = loads_json(content.decode("utf-8-sig"))

        except ValueError as error:  # Not UTF-8, not JSON, or a duplicate key.
            raise ValueError(f"The file is not valid JSON: {error}") from error

        name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(filename.replace("\\", "/")).name)[:80] or "file"

        return self.systems.import_system(data, source_detail=f"{name} (uploaded)")

    def overview(self) -> list[dict[str, Any]]:
        """One row per registered system, with what the home page shows about it."""
        rows = []

        for system in self.systems.list_systems():
            catalog = self.systems.catalog(system.system_id)
            runs = self.list_runs(system.system_id)
            rows.append({
                "system": system, "requirements": len(catalog.requirements) if catalog else 0,
                "version": catalog.specification_version if catalog else None,
                "runs": len(runs), "last_run": runs[0] if runs else None,
            })

        return rows

    def delete_system(self, system_id: str, *, delete_runs: bool = False) -> int:
        """Remove a system's registration (and, if asked, its stored investigations).

        A reset for a test installation. Investigations keep their own copy of the catalog, so they
        stay readable if kept, and a system registered again under the same name lists them again.
        Returns how many investigations were deleted. Nothing is deleted while one of this
        system's investigations is running. The SQLite checkpoint file is not touched.
        """
        self.systems.get(system_id)
        runs = self.list_runs(system_id)

        if any(run["execution"] == "running" for run in runs):
            raise Busy("An investigation of this system is running; stop it or wait for it first.")

        deleted = 0

        if delete_runs:
            for run in runs:
                directory = self.runs_path / run["run_id"]

                if reports.valid_run_id(run["run_id"]) and directory.parent == self.runs_path:
                    shutil.rmtree(directory)
                    deleted += 1

        self.systems.delete(system_id)

        return deleted

    def run_limits(self) -> dict[str, Any]:
        """The configured limits, shown before a run; empty if the settings cannot be read."""

        try:
            return dict(load_settings(self.directory / self.settings)["execution"])

        except (OSError, ValueError, KeyError):
            return {}

    # ---- validation -------------------------------------------------------
    def validate(
        self, system_id: str, version: str | None, endpoints: list[tuple[str, str]],
        requirement_ids: list[str], setup: list[tuple[str, str]] | None = None, focus: str = "",
    ) -> Validation:
        """Check a request without starting anything or reading any credential."""
        result = Validation(focus=focus.strip())

        try:
            system = self.systems.get(system_id)
            catalog = self.systems.catalog(system_id, version)

        except (LookupError, ValueError) as error:
            result.errors.append(str(error) or "Choose a registered system.")

            return result

        result.system = system

        if not system.target.runnable:
            result.errors.append("This system's target is external. Running against an external target "
                                 "needs an operational review and is not available from this application.")

        if catalog is None:
            result.errors.append("This system has no requirements yet. Add at least one first.")

            return result

        result.version = catalog.specification_version

        under_test = []

        for method, path in dict.fromkeys(endpoints):
            endpoint = system.endpoint(method, path)

            if endpoint is None:
                result.errors.append(f"{method} {path} is not a registered endpoint of this system.")

            else:
                under_test.append(endpoint)

        if not endpoints:
            result.errors.append("Choose the endpoint you want to test.")

        result.endpoints = under_test

        rules = {rule.id: rule for rule in catalog.requirements}
        chosen = [rules[i] for i in dict.fromkeys(requirement_ids) if i in rules]

        if len(chosen) != len(set(requirement_ids)):
            result.errors.append("Some requirements are not in this catalog version.")

        if not chosen:
            result.errors.append("Choose the requirement to verify.")

        tested = {(e.method, e.path) for e in under_test}

        for rule in chosen:
            if not tested & {(op.method, op.path) for op in rule.operations}:
                result.errors.append(f"{rule.id} does not apply to the chosen endpoint(s).")

        result.requirements = chosen

        registered = {(e.method, e.path) for e in system.endpoints}

        for method, path in dict.fromkeys(setup or []):
            if (method, path) not in registered:
                result.errors.append(f"{method} {path} is not a registered endpoint of this system.")

        authorized = [pair for pair in dict.fromkeys([*endpoints, *(setup or [])]) if pair in registered]
        covered = {(op.method, op.path) for rule in catalog.requirements for op in rule.operations}

        for method, path in authorized:
            if (method, path) not in covered:
                result.errors.append(
                    f"{method} {path} is not covered by any requirement of {catalog.specification_version}; "
                    "add a requirement for it before authorizing it.")

        if len(result.focus) > MAX_FOCUS_CHARS:
            result.errors.append(f"The extra detail cannot exceed {MAX_FOCUS_CHARS} characters.")

        if not result.errors:
            try:
                _, result.operations = prepare_request(
                    self.systems.catalog_path(system_id, catalog.specification_version),
                    catalog.specification_version, authorized)

            except (ValueError, OSError) as error:
                result.errors.append(str(error))

        if not result.errors:
            result.objective = build_objective(system, catalog.specification_version, under_test, chosen, result.focus)

            if len(result.objective) > MAX_OBJECTIVE_CHARS:
                result.errors.append("The selected requirements make the instruction too long. Choose fewer.")

            result.context = {
                "system_id": system.system_id, "system_name": system.name,
                "catalog_version": catalog.specification_version,
                "endpoints_under_test": [{"method": e.method, "path": e.path, "purpose": e.purpose} for e in under_test],
                "authorized_operations": [op.model_dump(mode="json") for op in result.operations],
                "requirement_ids": [rule.id for rule in chosen],
                "extra_focus": result.focus or None,
                "requested_at": datetime.now(UTC).isoformat(),
            }

        if not self.seed_for(system).is_file():
            result.errors.append("The sandbox seed file was not found.")

        try:
            settings = load_settings(self.directory / self.settings)

        except (OSError, ValueError, KeyError) as error:
            result.errors.append(f"The agent settings are not valid: {type(error).__name__}.")

        else:
            result.limits = dict(settings["execution"])

            for name in ("planner", "executor", "verifier"):
                profile = settings["llm"][settings["agents"][name]]

                # Only existence is checked; the file's content is never read here.
                if not Path(profile["credentials_file"]).is_file():
                    result.errors.append(
                        f"The credentials file for profile '{settings['agents'][name]}' ({name}) is missing.")

        result.errors = list(dict.fromkeys(result.errors))

        return result

    # ---- execution --------------------------------------------------------
    @property
    def active_run_id(self) -> str | None:
        if self._active is None or self._active[1].done():
            return None

        return self._active[0].run_id

    def start(
        self, system_id: str, version: str | None, endpoints: list[tuple[str, str]],
        requirement_ids: list[str], setup: list[tuple[str, str]] | None = None, focus: str = "",
    ) -> str:
        """Start an investigation in the background and return its run ID."""

        with self._lock:
            if self._active is not None and not self._active[1].done():
                raise Busy("An investigation is already active; wait for it to finish or stop it.")

            validation = self.validate(system_id, version, endpoints, requirement_ids, setup, focus)

            if not validation.ok:
                raise ValueError(" ".join(validation.errors))

            catalog = self.systems.catalog(system_id, validation.version)
            config = RunConfig(
                working_directory=self.directory, objective=validation.objective, settings=self.settings,
                runs_path=self.runs_path, checkpoint_path=self.checkpoint_path, sandbox=True,
                seed_file=self.seed_for(validation.system), context=validation.context,
            )
            control = RunControl()

            if self._loop is None:
                self._loop = _RunLoop()

            future = self._loop.submit(run_investigation(config, catalog, validation.operations, control=control))
            self._active = (control, future)

        while not control.created.wait(0.05):
            if future.done():
                future.result()  # Raises why the run could not even be created.
                raise RuntimeError("The investigation ended before it was recorded.")

        return control.run_id

    def request_stop(self, run_id: str) -> None:
        if self._active is None or self._active[1].done() or self._active[0].run_id != run_id:
            raise LookupError("That investigation is not active in this process.")

        self._active[0].request_stop()

    def wait(self, timeout: float | None = None) -> None:
        """Block until the active investigation ends (used by tests and shutdown)."""

        if self._active is not None:
            try:
                self._active[1].result(timeout)

            except concurrent.futures.TimeoutError:
                raise

            except Exception:
                pass  # Recorded in status.json.

    def close(self, timeout: float = 30) -> None:
        """Ask the active run to stop, wait for it and release the loop thread."""

        if self._active is not None and not self._active[1].done():
            self._active[0].request_stop()

            try:
                self.wait(timeout)

            except concurrent.futures.TimeoutError:
                self._active[1].cancel()

        if self._loop is not None:
            self._loop.stop()
            self._loop = None

    # ---- stored runs ------------------------------------------------------
    def list_runs(self, system_id: str | None = None) -> list[dict[str, Any]]:
        rows = reports.list_runs(self.runs_path, active_run_id=self.active_run_id)

        if system_id is None:
            return rows

        return [row for row in rows if (row["investigation"] or {}).get("system_id") == system_id]

    def view(self, run_id: str) -> dict[str, Any]:
        if not reports.valid_run_id(run_id):
            raise LookupError("Invalid investigation identifier.")

        try:
            return reports.load_view(self.runs_path, run_id, active_run_id=self.active_run_id)

        except FileNotFoundError as error:
            raise LookupError("The investigation does not exist.") from error

    def decide_stop(self, run_id: str, reviewer: str, resolution: str) -> None:
        """Close a paused run with a supervisor's `stop` decision (the CLI's own mechanism)."""
        view = self.view(run_id)

        if view["execution"] != "pending_review" or not view["review"]:
            raise ValueError("This investigation is not pending review.")

        if view["decision"]:
            raise ValueError("This investigation already has a recorded decision.")

        try:
            register_decision(self.runs_path, run_id, {
                "run_id": run_id, "reviewer": reviewer, "action": "stop", "resolution": resolution,
            })

        except ValidationError as error:
            raise ValueError("State who is reviewing and the reason for closing.") from error
