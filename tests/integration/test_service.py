import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage

from testing_agent.domain.requirements import OperationReference
from testing_agent.persistence.runs import create_run
from testing_agent.persistence.status import StatusWriter, read_status
from testing_agent_app.service import reports
from testing_agent_app.service.investigations import Busy, InvestigationService
from testing_agent_app.service.systems import Endpoint, SystemStore, Target
from tests.integration.test_experiment_flow import GET, ScriptedModel
from tests.integration.test_mvp_evaluation import ambiguous_catalog, assess, plan, run_case


ROOT = Path(__file__).resolve().parents[2]
PRODUCT = ("GET", "/products/{product_id}")
CANCEL = ("POST", "/reservations/{reservation_id}/cancel")


def local_settings(area: Path) -> Path:
    """Settings whose credentials file is a stub: no LLM is ever built from it."""
    (area / "creds.json").write_text('{"api_key": "fixture"}', encoding="utf-8")
    settings = {
        "llm": {"default": {"provider": "openai", "model": "fixture",
                            "credentials_file": "creds.json", "options": {}}},
        "agents": {"planner": "default", "executor": "default", "verifier": "default"},
        "execution": {"max_iterations": 3, "max_tool_calls": 10, "timeout_seconds": 120},
    }
    path = area / "settings.json"
    path.write_text(json.dumps(settings), encoding="utf-8")

    return path


def make_service(area: Path, runs: Path | None = None) -> InvestigationService:
    return InvestigationService(
        ROOT, settings=local_settings(area), runs_path=runs or area / "runs",
        checkpoint_path=area / "checkpoints.sqlite", systems_path=area / "systems",
    )


def stopped_before_any_operation(context):
    return {
        "evaluation": "Inconclusive. Stopped before any operation.",
        "findings": [{
            "requirement_id": context["requirements"][0]["id"], "outcome": "inconclusive",
            "scope": "No operation was executed.", "assessment": "No observations are available.",
            "evidence": [], "limitations": ["The user stopped the investigation before any operation."],
        }],
        "next_action": "executor", "review": None,
    }


def read_call() -> AIMessage:
    arguments = {"specification_version": "v1", **GET, "path_parameters": {"product_id": "product-001"}}

    return AIMessage(content="", tool_calls=[{"id": "call-0", "name": "request_application", "args": arguments}])


@pytest.fixture(scope="module")
def stored(tmp_path_factory):
    """A completed run and a run paused for review, produced through the real CLI."""
    area = tmp_path_factory.mktemp("stored")
    valid, _, _ = run_case("valid", area)
    paused, _, _ = run_case("ambiguity", area, ambiguous_catalog(area))

    return area, valid["run_id"], paused["run_id"]


def copy_area(area: Path, target: Path) -> Path:
    shutil.copytree(area, target)

    return target


# ---- status record --------------------------------------------------------
def test_status_writer_records_confirmed_milestones_atomically(tmp_path):
    run_id = create_run(tmp_path, "Synthetic objective", "v1")["run_id"]
    status = StatusWriter(tmp_path, run_id)
    status.milestone("run_started")
    status.update(iteration=1, evidence_records=2)
    assert read_status(tmp_path, run_id)["milestones"][0]["event"] == "run_started"

    with pytest.raises(ValueError):
        status.milestone("made_up_event")

    with pytest.raises(ValueError):
        status.finish("running")

    with pytest.raises(ValueError):
        status.update(unknown_field=1)

    status.finish("failed", error="boom")
    final = read_status(tmp_path, run_id)
    assert (final["execution"], final["error"], final["iteration"]) == ("failed", "boom", 1)
    assert not list((tmp_path / run_id).glob("status-*.tmp"))


def test_completed_run_leaves_confirmed_milestones_and_a_final_state(stored):
    area, valid, _ = stored
    status = read_status(area, valid)
    assert status["execution"] == "closed" and status["evidence_records"] == 1
    assert [item["event"] for item in status["milestones"]] == [
        "run_started", "target_ready", "plan_ready", "execution_recorded", "evaluation_done",
    ]


# ---- read model -------------------------------------------------------------
def test_report_resolves_citations_without_touching_the_trace(stored):
    area, valid, _ = stored
    trace = area / valid / "evidence" / "trace.json"
    before = trace.read_bytes()
    view = reports.load_view(area, valid)
    assert (view["execution"], view["case_outcome"]) == ("closed", "no_violation_observed")
    assert view["investigation"] is None  # Started from the CLI, not for a registered system.
    reference = view["findings"][0]["references"][0]
    assert reference["resolved"] and reference["call"]["status_code"] == 200
    assert json.loads(reference["call"]["response_body"])["id"] == "product-001"
    assert view["findings"][0]["requirement"]["expected_behavior"]
    assert trace.read_bytes() == before


def test_a_citation_without_a_matching_response_is_not_shown_as_evidence(stored, tmp_path):
    area, valid, _ = stored
    copy = copy_area(area, tmp_path / "copy")
    path = copy / valid / "reports" / "outcome.json"
    outcome = json.loads(path.read_text(encoding="utf-8"))
    outcome["findings"][0]["evidence"][0]["tool_call_id"] = "call-that-never-happened"
    path.write_text(json.dumps(outcome), encoding="utf-8")
    reference = reports.load_view(copy, valid)["findings"][0]["references"][0]
    assert not reference["resolved"] and reference["call"] is None


def test_execution_state_is_separate_from_the_business_outcome(tmp_path):
    def new_run(name):
        return create_run(tmp_path, name, "v1")["run_id"]

    legacy = new_run("legacy")  # Predates status.json; closed with an inconclusive result.
    (tmp_path / legacy / "reports" / "outcome.json").write_text(json.dumps(
        {"status": "closed", "case_outcome": "inconclusive", "stop_reason": None, "findings": []}))
    live, failed, bare = new_run("live"), new_run("failed"), new_run("bare")
    StatusWriter(tmp_path, live)
    StatusWriter(tmp_path, failed).finish("failed", error="X")
    (tmp_path / "not-a-run").mkdir()

    rows = {row["run_id"]: row for row in reports.list_runs(tmp_path, active_run_id=live)}
    assert len(rows) == 4
    assert (rows[legacy]["execution"], rows[legacy]["case_outcome"]) == ("closed", "inconclusive")
    assert rows[live]["execution"] == "running" and rows[failed]["execution"] == "failed"
    assert rows[bare]["execution"] == "interrupted"
    # The same "running" record is interrupted when this process is not executing it.
    assert {r["run_id"]: r for r in reports.list_runs(tmp_path)}[live]["execution"] == "interrupted"


def test_paused_run_closes_once_with_a_stop_decision(stored, tmp_path):
    area, _, paused = stored
    service = make_service(tmp_path, runs=copy_area(area, tmp_path / "copy"))
    view = service.view(paused)
    assert view["execution"] == "pending_review" and view["review"]["request"]["fragments"]
    service.decide_stop(paused, "Fixture reviewer", "Close this synthetic case.")
    assert service.view(paused)["decision"]["action"] == "stop"

    with pytest.raises(ValueError):
        service.decide_stop(paused, "Fixture reviewer", "Again.")

    with pytest.raises(ValueError):
        service.decide_stop(paused, "", "")  # Pydantic rejects it before anything is written.


# ---- system registry ----------------------------------------------------------
def test_example_system_is_registered_from_its_file_with_visible_provenance(tmp_path):
    service = make_service(tmp_path)
    assert service.systems.list_systems() == []  # Nothing is pre-registered.
    record = service.import_example()
    assert (record.source, record.source_detail) == ("imported", "config/examples/inventory_system.json")
    assert record.target.runnable and len(record.endpoints) == 4 and all(e.purpose for e in record.endpoints)
    catalog = service.systems.catalog(record.system_id)
    assert catalog.specification_version == "v1" and len(catalog.requirements) == 6
    assert service.systems.list_systems()[0].system_id == record.system_id


def system_file(**changes) -> dict:
    """A self-contained system file: the example system with its catalog written inside the file."""
    data = json.loads((ROOT / "config/examples/inventory_system.json").read_text(encoding="utf-8"))
    data["requirements"] = json.loads((ROOT / data["requirements"]).read_text(encoding="utf-8"))

    return {**data, **changes}


def upload(service, data, name="mine.json"):
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()

    return service.import_upload(raw, name)


def test_a_system_can_be_registered_from_an_uploaded_file_that_carries_its_own_catalog(tmp_path):
    service = make_service(tmp_path)
    record = upload(service, system_file(name="Uploaded inventory"), r"C:\Users\someone\my `file`.json")
    assert (record.name, record.source) == ("Uploaded inventory", "imported")
    assert record.source_detail == "my _file_.json (uploaded)"  # Only the sanitized base name is kept.
    assert len(record.endpoints) == 4 and len(service.systems.catalog(record.system_id).requirements) == 6
    # A file without requirements registers the system alone; they can be added afterwards.
    bare = system_file(name="Bare")
    del bare["requirements"]
    assert service.systems.catalog(upload(service, bare).system_id) is None


def test_uploaded_files_are_rejected_with_a_message_and_leave_nothing_registered(tmp_path):
    service = make_service(tmp_path)
    unknown_endpoint = system_file()
    unknown_endpoint["endpoints"] = unknown_endpoint["endpoints"][:1]
    cases = {
        "not valid JSON": b"{ nope",
        "Duplicate JSON key": b'{"name": "a", "name": "b"}',
        "one JSON object": b"[1, 2]",
        "missing these fields": json.dumps({"name": "x"}).encode(),
        "Unknown fields": json.dumps(system_file(extra=1)).encode(),
        "must be text": json.dumps(system_file(name=3)).encode(),
        "not as the path of another file": json.dumps({**system_file(), "requirements": "specifications/target_app/v1/requirements.json"}).encode(),
        "Register these endpoints first": json.dumps(unknown_endpoint).encode(),
        "sensitive values": json.dumps(system_file(purpose="Use password: hunter2 to log in.")).encode(),
        "larger than": b" " * 600_000,
        "not valid JSON: ": b"\xff\xfe\x00",
    }

    for message, content in cases.items():
        with pytest.raises(ValueError, match=message):
            service.import_upload(content, "bad.json")

    assert service.systems.list_systems() == []


def test_example_files_are_chosen_by_name_and_a_file_cannot_pick_what_else_is_read(tmp_path):
    service = make_service(tmp_path)
    assert service.example_files() == ["config/examples/inventory_system.json",
                                       "config/examples/inventory_system_variant_b.json"]

    for outside in ("config/application.json", "../outside.json", "config/examples/missing.json"):
        with pytest.raises(ValueError, match="example files"):
            service.import_example(outside)

    base = tmp_path / "project"
    (base / "config").mkdir(parents=True)
    (tmp_path / "outside.json").write_text("{}", encoding="utf-8")
    pointing_out = base / "config" / "system.json"
    pointing_out.write_text(json.dumps({**system_file(), "requirements": "../outside.json"}), encoding="utf-8")

    with pytest.raises(ValueError, match="inside the project"):
        service.systems.import_system_file(pointing_out, base=base)

    assert service.systems.list_systems() == []


def test_requirement_versions_are_immutable_and_use_registered_endpoints(tmp_path):
    store = SystemStore(tmp_path)
    system = store.create("Payments", "Approves and captures payments.", Target(kind="external", base_url="https://sandbox.example.test"),
                          [Endpoint(method="POST", path="/payments/{payment_id}/amount", purpose="Changes an amount.")])
    assert store.catalog(system.system_id) is None
    operation = OperationReference(method="POST", path="/payments/{payment_id}/amount")
    first = store.add_requirement(system.system_id, requirement_id="REQ-PAY-001", description="Amount of an approved payment is final.",
                                  operations=[operation], expected_behavior="The amount of an approved payment cannot change.")
    snapshot = store.catalog_path(system.system_id, first).read_bytes()
    second = store.add_requirement(system.system_id, requirement_id="REQ-PAY-002", description="Pending amounts may change once.",
                                   operations=[operation], expected_behavior="One change is allowed while pending.")
    assert (first, second) == ("v1", "v2")
    assert store.catalog_path(system.system_id, first).read_bytes() == snapshot
    assert [r.id for r in store.catalog(system.system_id).requirements] == ["REQ-PAY-001", "REQ-PAY-002"]
    assert [r.id for r in store.catalog(system.system_id, "v1").requirements] == ["REQ-PAY-001"]

    with pytest.raises(ValueError, match="unique"):
        store.add_requirement(system.system_id, requirement_id="REQ-PAY-001", description="Duplicate.",
                              operations=[operation], expected_behavior="x")

    with pytest.raises(ValueError, match="Register these endpoints first"):
        store.add_requirement(system.system_id, requirement_id="REQ-PAY-003", description="Unregistered.",
                              operations=[OperationReference(method="DELETE", path="/payments/{payment_id}")],
                              expected_behavior="x")

    assert store.versions(system.system_id) == ["v1", "v2"]  # Failed attempts leave no version behind.


def test_a_system_names_its_seed_and_the_service_runs_it_on_that_seed_only(tmp_path):
    service = make_service(tmp_path)
    plain = service.import_example()
    flawed = service.import_example("config/examples/inventory_system_variant_b.json")
    seeds = ROOT / "sandbox/target_app/seed"
    assert plain.target.seed is None and flawed.target.seed == "products_cancel_retry.json"
    assert service.seed_for(plain) == seeds / "products.json"
    assert service.seed_for(flawed) == seeds / "products_cancel_retry.json"
    assert service.seed_for(flawed).is_file()

    for bad in ("../products.json", "dir/products.json", "products", "..\\x.json"):
        with pytest.raises(ValueError):
            Target(kind="managed_sandbox", seed=bad)

    with pytest.raises(ValueError, match="managed sandbox"):
        Target(kind="external", base_url="https://host", seed="products.json")


def test_target_and_endpoint_registration_rules(tmp_path):
    store = SystemStore(tmp_path)

    for bad in ("ftp://host", "https://user:secret@host", "https://host/?token=1", "host"):
        with pytest.raises(ValueError):
            Target(kind="external", base_url=bad)

    with pytest.raises(ValueError):
        Target(kind="managed_sandbox", base_url="https://host")

    endpoint = Endpoint(method="GET", path="/items", purpose="Lists items.")

    with pytest.raises(ValueError, match="twice"):
        store.create("Dup", "x", Target(kind="managed_sandbox"), [endpoint, endpoint])

    assert store.list_systems() == []  # The failed registration left nothing behind.
    first = store.create("Same name", "x", Target(kind="managed_sandbox"), [endpoint])
    second = store.create("Same name", "x", Target(kind="managed_sandbox"), [endpoint])
    assert first.system_id != second.system_id


# ---- investigation requests ----------------------------------------------------
def test_validation_checks_the_request_against_the_registered_system(tmp_path):
    service = make_service(tmp_path)
    system = service.import_example().system_id
    ok = service.validate(system, None, [PRODUCT], ["REQ-PRODUCT-001"], focus="Use product-001.")
    assert ok.ok and ok.version == "v1" and [op.path for op in ok.operations] == [PRODUCT[1]]
    assert "Expected behavior according to the catalog" in ok.objective
    assert "Additional focus provided by the user (not part of the catalog): Use product-001." in ok.objective
    assert ok.context["requirement_ids"] == ["REQ-PRODUCT-001"] and ok.context["system_id"] == system

    def errors(*args, **kwargs):
        result = service.validate(*args, **kwargs)
        assert not result.ok

        return " ".join(result.errors)

    assert "not registered" in errors("nope", None, [PRODUCT], ["REQ-PRODUCT-001"])
    assert "Choose the endpoint" in errors(system, None, [], ["REQ-PRODUCT-001"])
    assert "Choose the requirement" in errors(system, None, [PRODUCT], [])
    assert "not a registered endpoint" in errors(system, None, [("GET", "/nope")], ["REQ-PRODUCT-001"])
    assert "does not apply" in errors(system, None, [PRODUCT], ["REQ-RESERVATION-004"])
    assert "not in this catalog version" in errors(system, None, [PRODUCT], ["REQ-NOPE"])
    assert "cannot exceed" in errors(system, None, [PRODUCT], ["REQ-PRODUCT-001"], focus="x" * 1001)
    assert "not a registered endpoint" in errors(system, None, [PRODUCT], ["REQ-PRODUCT-001"], setup=[("GET", "/nope")])

    both = service.validate(system, None, [CANCEL], ["REQ-RESERVATION-004"], setup=[PRODUCT, ("POST", "/reservations")])
    assert both.ok and len(both.operations) == 3  # Setup operations are authorized explicitly.

    (tmp_path / "creds.json").unlink()
    assert "credentials file" in errors(system, None, [PRODUCT], ["REQ-PRODUCT-001"])


def test_a_system_with_an_external_target_cannot_be_run_from_here(tmp_path):
    service = make_service(tmp_path)
    store = service.systems
    system = store.create("Payments", "x", Target(kind="external", base_url="https://sandbox.example.test"),
                          [Endpoint(method="GET", path="/payments/{payment_id}", purpose="Reads a payment.")])
    store.add_requirement(system.system_id, requirement_id="REQ-PAY-001", description="Reads.",
                          operations=[OperationReference(method="GET", path="/payments/{payment_id}")],
                          expected_behavior="Returns the payment.")
    result = service.validate(system.system_id, None, [("GET", "/payments/{payment_id}")], ["REQ-PAY-001"])
    assert not result.ok and "operational review" in " ".join(result.errors)

    with pytest.raises(ValueError):
        service.start(system.system_id, None, [("GET", "/payments/{payment_id}")], ["REQ-PAY-001"])

    assert service.active_run_id is None


# ---- running investigations ----------------------------------------------------
def test_investigation_for_a_registered_system_runs_on_its_own_thread_and_is_traceable(tmp_path):
    service = make_service(tmp_path)
    system = service.import_example().system_id
    models = {
        "planner": ScriptedModel([plan]),
        "executor": ScriptedModel([read_call(), AIMessage(content="Done.")]),
        "verifier": ScriptedModel([assess]),
    }

    try:
        with patch("testing_agent.bootstrap.create_model", lambda agent, settings: models[agent]):
            run_id = service.start(system, None, [PRODUCT], ["REQ-PRODUCT-001"], focus="Check stock only.")
            service.wait(120)

        view = service.view(run_id)
        assert (view["execution"], view["case_outcome"]) == ("closed", "no_violation_observed")
        assert view["investigation"]["system_id"] == system
        assert view["investigation"]["requirement_ids"] == ["REQ-PRODUCT-001"]
        assert view["investigation"]["extra_focus"] == "Check stock only."
        assert "Inventory & Reservations" in view["run"]["objective"]
        assert [r["run_id"] for r in service.list_runs(system)] == [run_id]
        assert service.list_runs("another-system") == []

    finally:
        service.close()


def test_the_investigation_of_a_system_starts_the_sandbox_on_that_systems_seed(tmp_path):
    from testing_agent.integrations.mcp.client import open_mcp_tools

    service = make_service(tmp_path)
    plain = service.import_example().system_id
    flawed = service.import_example("config/examples/inventory_system_variant_b.json").system_id
    started = []

    def spy(args, **kwargs):
        started.append(Path(args[args.index("--seed-file") + 1]).name)

        return open_mcp_tools(args, **kwargs)

    try:
        for system in (plain, flawed):
            models = {
                "planner": ScriptedModel([plan]),
                "executor": ScriptedModel([read_call(), AIMessage(content="Done.")]),
                "verifier": ScriptedModel([assess]),
            }

            with patch("testing_agent.bootstrap.create_model", lambda agent, settings: models[agent]), \
                    patch("testing_agent.bootstrap.open_mcp_tools", spy):
                service.start(system, None, [PRODUCT], ["REQ-PRODUCT-001"])
                service.wait(120)

        assert started == ["products.json", "products_cancel_retry.json"]

    finally:
        service.close()


def test_removing_a_system_keeps_or_deletes_its_investigations_on_request(tmp_path):
    service = make_service(tmp_path)
    system = service.import_example().system_id
    models = {
        "planner": ScriptedModel([plan]),
        "executor": ScriptedModel([read_call(), AIMessage(content="Done.")]),
        "verifier": ScriptedModel([assess]),
    }

    try:
        with patch("testing_agent.bootstrap.create_model", lambda agent, settings: models[agent]):
            run_id = service.start(system, None, [PRODUCT], ["REQ-PRODUCT-001"])
            service.wait(120)

        assert service.delete_system(system) == 0  # The registration goes; the evidence stays.
        assert service.systems.list_systems() == []
        assert [run["run_id"] for run in service.list_runs(system)] == [run_id]
        assert service.view(run_id)["case_outcome"] == "no_violation_observed"  # Still readable.

        again = service.import_example()  # The same path the "Load example system" button uses.
        assert again.system_id == system and len(service.systems.catalog(system).requirements) == 6
        assert service.delete_system(system, delete_runs=True) == 1
        assert service.list_runs() == [] and not (service.runs_path / run_id).exists()

        with pytest.raises(LookupError):
            service.delete_system(system)

    finally:
        service.close()


def test_a_system_cannot_be_removed_while_one_of_its_investigations_runs(tmp_path):
    service = make_service(tmp_path)
    system = service.import_example().system_id
    refused = []

    def planner(context):
        try:
            service.delete_system(system, delete_runs=True)

        except Busy as error:
            refused.append(str(error))

        service.request_stop(service.active_run_id)  # Wrap the run up quickly.

        return plan(context)

    models = {
        "planner": ScriptedModel([planner]),
        "executor": ScriptedModel([AIMessage(content="Never used.")]),
        "verifier": ScriptedModel([stopped_before_any_operation]),
    }

    try:
        with patch("testing_agent.bootstrap.create_model", lambda agent, settings: models[agent]):
            service.start(system, None, [PRODUCT], ["REQ-PRODUCT-001"])
            service.wait(120)

        assert refused and "is running" in refused[0]
        assert service.systems.get(system) and len(service.list_runs(system)) == 1  # Nothing was deleted.

    finally:
        service.close()


def test_user_stop_closes_without_operations_and_one_run_at_a_time(tmp_path):
    service = make_service(tmp_path)
    system = service.import_example().system_id
    models = {
        "planner": ScriptedModel([lambda ctx: (service.request_stop(service.active_run_id), plan(ctx))[1]]),
        "executor": ScriptedModel([AIMessage(content="Never used.")]),
        "verifier": ScriptedModel([stopped_before_any_operation]),
    }

    try:
        with patch("testing_agent.bootstrap.create_model", lambda agent, settings: models[agent]):
            run_id = service.start(system, None, [PRODUCT], ["REQ-PRODUCT-001"])

            with pytest.raises(Busy):
                service.start(system, None, [PRODUCT], ["REQ-PRODUCT-001"])

            with pytest.raises(LookupError):
                service.request_stop("00000000-0000-4000-8000-000000000000")

            service.wait(120)

        view = service.view(run_id)
        assert view["execution"] == "stopped" and view["stop_reason"] == "user_stopped"
        assert view["case_outcome"] == "inconclusive"
        assert [call for call in view["calls"] if call["tool_call_id"]] == []
        assert view["status"]["stop_requested"] is True
        assert "stop_requested" in [item["event"] for item in view["status"]["milestones"]]

    finally:
        service.close()
