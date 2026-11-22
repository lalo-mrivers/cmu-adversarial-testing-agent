import json
import re
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from testing_agent.domain.requirements import (
    HttpMethod,
    NonEmptyText,
    OperationReference,
    Requirement,
    RequirementCatalog,
    RequirementException,
)
from testing_agent.persistence.redaction import redact
from testing_agent.persistence.status import replace_file
from testing_agent.runner import read_json

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
VERSION = re.compile(r"^v([1-9][0-9]*)$")


def _first_error(error: ValidationError) -> str:
    detail = error.errors()[0]
    where = ".".join(str(part) for part in detail["loc"])

    return f"{where}: {detail['msg']}" if where else detail["msg"]


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    method: HttpMethod
    path: str = Field(pattern=r"^/[^?#\s]*$")
    purpose: NonEmptyText

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}"


class Target(BaseModel):
    """Where the system can be exercised. Registering a target does not authorize running on it."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal["managed_sandbox", "external"]
    base_url: str | None = None
    # Name of a seed file in sandbox/target_app/seed/ (a name, never a path). None: the default seed.
    seed: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+\.json$")

    @model_validator(mode="after")
    def _check(self) -> "Target":
        if self.kind == "managed_sandbox":
            if self.base_url is not None:
                raise ValueError("The managed sandbox has no base URL.")

            return self

        if self.seed is not None:
            raise ValueError("Only the managed sandbox has a seed.")

        if not self.base_url:
            raise ValueError("An external target needs a base URL.")

        parts = urlsplit(self.base_url)

        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("The base URL must start with http:// or https://.")

        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("The base URL cannot contain credentials, a query or a fragment.")

        return self

    @property
    def runnable(self) -> bool:
        """Only the managed sandbox can be exercised from this application."""

        return self.kind == "managed_sandbox"


class SystemRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    system_id: str = Field(pattern=SLUG.pattern)
    name: NonEmptyText
    purpose: NonEmptyText
    target: Target
    endpoints: list[Endpoint]
    created_at: NonEmptyText
    source: Literal["entered", "imported"]
    source_detail: str | None = None

    def endpoint(self, method: str, path: str) -> Endpoint | None:
        return next((e for e in self.endpoints if (e.method, e.path) == (method, path)), None)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40].strip("-")

    return slug or "system"


def _text(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _replace(path: Path, content: str) -> None:
    """Atomically replace a file (a reader never sees a partial write)."""

    with NamedTemporaryFile(dir=path.parent, prefix=path.name + "-", suffix=".tmp", delete=False) as file:
        temporary = Path(file.name)

    try:
        temporary.write_text(content, encoding="utf-8")
        replace_file(temporary, path)

    finally:
        temporary.unlink(missing_ok=True)


def _no_sensitive_values(value: Any, what: str) -> None:
    if redact(value) != value:
        raise ValueError(f"{what} contains sensitive values. Remove them before registering.")


class SystemStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    # ---- paths --------------------------------------------------------------
    def _directory(self, system_id: str) -> Path:
        if not SLUG.match(system_id):
            raise LookupError("Invalid system identifier.")

        return self.root / system_id

    def catalog_path(self, system_id: str, version: str) -> Path:
        if not VERSION.match(version):
            raise LookupError("Invalid catalog version.")

        return self._directory(system_id) / "requirements" / f"{version}.json"

    # ---- systems ------------------------------------------------------------
    def list_systems(self) -> list[SystemRecord]:
        if not self.root.is_dir():
            return []

        records = []

        for directory in self.root.iterdir():
            try:
                records.append(self.get(directory.name))

            except (LookupError, ValueError):
                continue  # Not a system folder.

        return sorted(records, key=lambda record: record.created_at)

    def get(self, system_id: str) -> SystemRecord:
        path = self._directory(system_id) / "system.json"

        try:
            return SystemRecord.model_validate(read_json(path))

        except FileNotFoundError as error:
            raise LookupError("That system is not registered.") from error

    def create(
        self, name: str, purpose: str, target: Target, endpoints: list[Endpoint], *,
        source: Literal["entered", "imported"] = "entered", source_detail: str | None = None,
    ) -> SystemRecord:
        base = _slug(name)
        self.root.mkdir(parents=True, exist_ok=True)

        for suffix in range(1, 100):
            system_id = base if suffix == 1 else f"{base}-{suffix}"

            try:
                (self.root / system_id).mkdir()
                break

            except FileExistsError:
                continue

        else:
            raise ValueError("Too many systems share that name. Choose another name.")

        try:
            keys = [endpoint.key for endpoint in endpoints]

            if len(keys) != len(set(keys)):
                raise ValueError("The same endpoint is listed twice.")

            record = SystemRecord(
                system_id=system_id, name=name, purpose=purpose, target=target, endpoints=endpoints,
                created_at=_now(), source=source, source_detail=source_detail,
            )
            _no_sensitive_values(record.model_dump(mode="json"), "The system description")
            (self.root / system_id / "requirements").mkdir()
            _replace(self.root / system_id / "system.json", _text(record.model_dump(mode="json")))

        except ValidationError as error:
            self._discard(system_id)
            raise ValueError(_first_error(error)) from error

        except Exception:
            self._discard(system_id)
            raise

        return record

    def _discard(self, system_id: str) -> None:
        """Remove a folder this call just created, after a failed registration."""
        directory = self.root / system_id

        for child in sorted(directory.rglob("*"), reverse=True):
            child.rmdir() if child.is_dir() else child.unlink()

        directory.rmdir()

    def delete(self, system_id: str) -> None:
        """Remove a registration with all its requirement versions (a reset for a test installation).

        Investigations stored elsewhere are not touched here: each keeps its own copy of the catalog.
        """
        directory = self._directory(system_id)
        self.get(system_id)  # Raises LookupError unless this really is a registered system.

        for attempt in range(10):  # A sync client or an open reader can hold a file for a moment on Windows.
            try:
                shutil.rmtree(directory)

                return

            except FileNotFoundError:
                return

            except PermissionError:
                if attempt == 9:
                    raise

                time.sleep(0.1 * (attempt + 1))

    def add_endpoint(self, system_id: str, endpoint: Endpoint) -> SystemRecord:
        record = self.get(system_id)

        if record.endpoint(endpoint.method, endpoint.path):
            raise ValueError("That endpoint is already registered.")

        updated = record.model_copy(update={"endpoints": [*record.endpoints, endpoint]})
        _no_sensitive_values(updated.model_dump(mode="json"), "The endpoint description")
        _replace(self._directory(system_id) / "system.json", _text(updated.model_dump(mode="json")))

        return updated

    # ---- requirement catalogs ---------------------------------------------------
    def versions(self, system_id: str) -> list[str]:
        folder = self._directory(system_id) / "requirements"
        found = [path.stem for path in folder.glob("*.json") if VERSION.match(path.stem)]

        return sorted(found, key=lambda version: int(version[1:]))

    def catalog(self, system_id: str, version: str | None = None) -> RequirementCatalog | None:
        """The requested version, the latest one, or None when no requirement is registered yet."""
        versions = self.versions(system_id)

        if not versions:
            return None

        chosen = version or versions[-1]

        if chosen not in versions:
            raise LookupError("That catalog version does not exist.")

        catalog = RequirementCatalog.model_validate(read_json(self.catalog_path(system_id, chosen)))

        if catalog.specification_version != chosen:
            raise ValueError(f"The file for {chosen} declares version {catalog.specification_version}.")

        return catalog

    def _write_catalog(self, record: SystemRecord, catalog: RequirementCatalog) -> None:
        registered = {endpoint.key for endpoint in record.endpoints}
        used = {f"{op.method} {op.path}" for rule in catalog.requirements for op in rule.operations}

        if not used <= registered:
            raise ValueError(
                "Register these endpoints first: " + ", ".join(sorted(used - registered)) + ".")

        _no_sensitive_values(catalog.model_dump(mode="json"), "The requirements")
        path = self.catalog_path(record.system_id, catalog.specification_version)

        with path.open("x", encoding="utf-8") as file:  # Versions are immutable: never overwritten.
            file.write(_text(catalog.model_dump(mode="json")))

    def add_requirement(
        self, system_id: str, *, requirement_id: str, description: str,
        operations: list[OperationReference], expected_behavior: str,
        preconditions: list[str] | None = None, exceptions: list[RequirementException] | None = None,
    ) -> str:
        """Add a requirement by writing a new catalog version; returns that version."""
        record = self.get(system_id)
        current = self.catalog(system_id)
        version = f"v{int(current.specification_version[1:]) + 1}" if current else "v1"

        try:
            rule = Requirement(
                id=requirement_id, description=description, operations=operations,
                preconditions=preconditions or [], expected_behavior=expected_behavior,
                exceptions=exceptions or [],
            )
            catalog = RequirementCatalog(
                specification_version=version,
                requirements=[*(current.requirements if current else []), rule],
            )

        except ValidationError as error:
            raise ValueError(_first_error(error)) from error

        self._write_catalog(record, catalog)

        return version

    def import_catalog(self, system_id: str, catalog: RequirementCatalog) -> str:
        """Register an existing catalog under its own version (it must not exist yet)."""

        if not VERSION.match(catalog.specification_version):
            raise ValueError("The catalog version must look like v1, v2, …")

        if catalog.specification_version in self.versions(system_id):
            raise ValueError(f"Version {catalog.specification_version} already exists.")

        self._write_catalog(self.get(system_id), catalog)

        return catalog.specification_version

    def import_system_file(self, path: str | Path, *, base: str | Path) -> SystemRecord:
        """Register a system described by a JSON file inside the project (the example files).

        The record keeps `source: imported` and the file's path, so nothing looks like it was
        analyzed or entered by hand. `requirements` may name a catalog file under `base` or hold
        the catalog itself.
        """
        path, base = Path(path), Path(base)
        detail = path.relative_to(base).as_posix() if path.is_relative_to(base) else path.name

        return self.import_system(read_json(path), source_detail=detail, base=base)

    def import_system(self, data: Any, *, source_detail: str, base: Path | None = None) -> SystemRecord:
        """Register a system from its parsed description.

        Fields: `name`, `purpose`, `target`, `endpoints` and, optionally, `requirements`: either
        the catalog itself (a JSON object) or the path of a catalog file under `base`. An upload
        has no `base`, so it must carry its catalog inline: a file must not choose which other
        file of this computer is read.
        """
        required = ("name", "purpose", "target", "endpoints")

        if not isinstance(data, dict):
            raise ValueError("The file must contain one JSON object describing the system.")

        if missing := [field for field in required if field not in data]:
            raise ValueError(f"The file is missing these fields: {missing}.")

        if unknown := set(data) - {*required, "requirements"}:
            raise ValueError(f"Unknown fields in the file: {sorted(unknown)}.")

        if not isinstance(data["name"], str) or not isinstance(data["purpose"], str):
            raise ValueError("`name` and `purpose` must be text.")

        if not isinstance(data["endpoints"], list):
            raise ValueError("`endpoints` must be a list.")

        requirements = data.get("requirements")

        try:
            if isinstance(requirements, str):
                if base is None:
                    raise ValueError("An uploaded file must include its requirements as a JSON object, "
                                     "not as the path of another file.")

                location = (base / requirements).resolve()

                if not location.is_relative_to(base.resolve()):
                    raise ValueError("The requirements file must be inside the project folder.")

                requirements = read_json(location)

            elif requirements is not None and not isinstance(requirements, dict):
                raise ValueError("`requirements` must be a catalog object or a file path.")

            catalog = RequirementCatalog.model_validate(requirements) if requirements is not None else None
            record = self.create(
                data["name"], data["purpose"], Target.model_validate(data["target"]),
                [Endpoint.model_validate(item) for item in data["endpoints"]],
                source="imported", source_detail=source_detail,
            )

        except ValidationError as error:
            raise ValueError(_first_error(error)) from error

        if catalog is not None:
            try:
                self.import_catalog(record.system_id, catalog)

            except Exception:
                self._discard(record.system_id)
                raise

        return record
