import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage
from streamlit.testing.v1 import AppTest

from testing_agent_app.ui.common import plain, report_markdown
from tests.integration.test_experiment_flow import ScriptedModel
from tests.integration.test_mvp_evaluation import assess, plan
from tests.integration.test_service import (  # noqa: F401  (stored is a fixture)
    PRODUCT, copy_area, make_service, read_call, stored,
)

APP = Path(__file__).resolve().parents[2] / "src" / "testing_agent_app" / "ui" / "app.py"


def open_app(service, page: str | None = None, **query) -> AppTest:
    """Open the app (the Systems page) or one of its pages with the given address parameters."""
    test = AppTest.from_file(str(APP), default_timeout=60)
    test.session_state["_service"] = service
    test.run()  # Registers the pages.

    if page:
        for key, value in query.items():
            test.query_params[key] = value

        test.switch_page(f"app_pages/{page}.py").run()

    return test


def texts(test: AppTest) -> str:
    """All visible text, so a test can say what a person would read."""
    parts = []

    for group in (test.title, test.header, test.subheader, test.markdown, test.caption, test.success,
                  test.info, test.warning, test.error):
        parts += [element.value for element in group]
        parts += [element.proto.title for element in group if getattr(element.proto, "title", "")]

    return re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", "\n".join(parts))  # Markdown renders escapes as the character.


def test_plain_text_cannot_become_a_link_an_image_or_html():
    hostile = "![x](http://evil.test/a.png) <script>alert(1)</script> [click](http://evil.test) www.evil.test :red[x]"
    rendered = plain(hostile)
    unescaped = re.sub(r"\\.", "", rendered)  # What is left once every escaped pair is removed.
    assert not re.search(r"[!-/:-@\[-`{-~]", unescaped)
    assert "\\!\\[x\\]\\(http\\:\\/\\/evil\\.test" in rendered


def test_home_tells_what_the_tool_does_and_shows_real_counts(tmp_path):
    service = make_service(tmp_path)
    home = open_app(service)  # The default page.
    assert not home.exception
    page = texts(home)

    for expected in ("Does your application really follow its business rules?", "How you use it", "Planner",
                     "Executor", "Verifier", "Safe by design", "What a verdict means", "Inconclusive"):
        assert expected in page

    assert [metric.value for metric in home.metric] == ["0", "0", "0"]  # Nothing is pre-registered.
    service.import_example()
    assert [metric.value for metric in open_app(service).metric] == ["1", "6", "0"]


def test_a_system_can_be_removed_and_registered_again_from_the_example_file(tmp_path):
    service = make_service(tmp_path)
    system = service.import_example()
    test = open_app(service, "system", id=system.system_id)
    assert test.button(key="remove-system").disabled  # Needs the confirmation first.
    test.checkbox(key=f"remove-sure-{system.system_id}").check().run()
    test.button(key="remove-system").click().run()
    assert not test.exception and service.systems.list_systems() == []

    systems = open_app(service, "systems")
    assert systems.selectbox[0].options == ["config/examples/inventory_system.json",
                                                 "config/examples/inventory_system_variant_b.json"]  # Offered again.
    assert service.import_example().system_id == system.system_id


def test_an_empty_installation_offers_registration_and_does_not_pretend_to_know_any_system(tmp_path):
    service = make_service(tmp_path)
    test = open_app(service, "systems")
    assert not test.exception
    page = texts(test)
    assert "No system is registered yet" in page and "Expected format" in page
    assert "Inventory & Reservations" not in page  # The example is not announced as if it were known.
    assert test.selectbox[0].options == ["config/examples/inventory_system.json",
                                         "config/examples/inventory_system_variant_b.json"]
    assert service.systems.list_systems() == []  # Looking at the page registers nothing.

    test.button(key="load-example").click().run()
    assert not test.exception and service.systems.list_systems()[0].name == "Inventory & Reservations"
    page = texts(test)
    assert "Registered" in page and "imported from" in page and "config/examples/inventory_system.json" in page


def test_a_system_file_can_be_uploaded_and_a_bad_one_is_explained(tmp_path):
    service = make_service(tmp_path)
    test = open_app(service, "systems")
    test.file_uploader(key="system-upload").set_value(("broken.json", b"{ nope", "application/json")).run()
    test.button(key="register-upload").click().run()
    assert not test.exception and service.systems.list_systems() == []
    assert "not valid JSON" in "\n".join(error.value for error in test.error)

    data = json.loads((Path(APP).parents[3] / "config/examples/inventory_system.json").read_text(encoding="utf-8"))
    data["name"] = "Uploaded system"
    data["requirements"] = json.loads((Path(APP).parents[3] / data["requirements"]).read_text(encoding="utf-8"))
    test.file_uploader(key="system-upload").set_value(("mine.json", json.dumps(data).encode(), "application/json")).run()
    test.button(key="register-upload").click().run()
    assert not test.exception
    record = service.systems.list_systems()[0]
    assert (record.name, record.source_detail) == ("Uploaded system", "mine.json (uploaded)")
    page = texts(test)
    assert "Registered" in page and "imported from" in page and "mine.json (uploaded)" in page


def test_system_page_lists_endpoints_requirements_and_starts_an_investigation_for_one_endpoint(tmp_path):
    service = make_service(tmp_path)
    system = service.import_example()
    test = open_app(service, "system", id=system.system_id)
    assert not test.exception
    page = texts(test)
    assert "Covered by REQ-PRODUCT-001" in page and "Returns a product with its identifier" in page
    assert any(tab.label == "Requirements" for tab in test.tabs)
    assert any("REQ-RESERVATION-004" in expander.label for expander in test.expander)

    test.button(key=f"test-GET /products/{{product_id}}").click().run()
    assert not test.exception
    assert test.multiselect[0].value == ["GET /products/{product_id}"]
    assert test.multiselect[1].value == ["REQ-PRODUCT-001"]  # The rule the endpoint fully covers.
    test.switch_page("app_pages/investigate.py")  # The script navigated; the test runner must follow.
    assert not test.button(key="run").disabled
    assert "Expected behavior according to the catalog" in "\n".join(code.value for code in test.code)


def test_investigation_page_explains_what_blocks_a_run(tmp_path):
    service = make_service(tmp_path)
    system = service.import_example()
    test = open_app(service, "investigate", id=system.system_id)
    assert not test.exception
    assert "Choose the endpoint you want to test." in texts(test)
    assert test.button(key="run").disabled


def test_full_flow_from_the_interface_to_a_stored_traceable_report(tmp_path):
    service = make_service(tmp_path)
    system = service.import_example()
    models = {
        "planner": ScriptedModel([plan]),
        "executor": ScriptedModel([read_call(), AIMessage(content="Done.")]),
        "verifier": ScriptedModel([assess]),
    }

    try:
        with patch("testing_agent.bootstrap.create_model", lambda agent, settings: models[agent]):
            test = open_app(service, "investigate", id=system.system_id, endpoint="GET /products/{product_id}")
            test.text_area(key=f"inv-focus-{system.system_id}").set_value("Check the stock only.").run()
            test.button(key="run").click().run()
            assert not test.exception
            run_id = test.query_params["id"]
            service.wait(120)

        done = open_app(service, "run", id=run_id)
        assert not done.exception
        page = texts(done)
        assert "Verdict: No violation observed within the tested scope" in page
        assert "Closed" in page and "Check the stock only." in page and "not part of the catalog" in page
        assert "REQ-PRODUCT-001" in page and "External changes were not excluded" in page
        assert any("HTTP 200" in expander.label for expander in done.expander)

        listing = open_app(service, "system", id=system.system_id)
        assert "GET /products/{product_id} → REQ-PRODUCT-001" in texts(listing)
        assert "Open" in [button.label for button in listing.button]  # The investigation is listed.

        report = report_markdown(service.view(run_id))
        assert "## Verdict: No violation observed within the tested scope" in report
        assert "Extra detail added by the user (not part of the catalog): Check the stock only." in report
        assert "product-001" in report and "Recommendations" not in report

    finally:
        service.close()


def test_a_paused_investigation_shows_the_question_and_closes_with_a_decision(stored, tmp_path):  # noqa: F811
    area, _, paused = stored
    service = make_service(tmp_path, runs=copy_area(area, tmp_path / "copy"))
    test = open_app(service, "run", id=paused)
    assert not test.exception
    page = texts(test)
    assert "A human decision is needed" in page and "Question that needs a decision" in page
    test.text_input[0].set_value("Fixture reviewer")
    test.text_area[0].set_value("Close this synthetic case.")
    test.button[-1].click().run() if False else None  # The form's submit button is the last one.
    submit = [button for button in test.button if button.label == "Close case"][0]
    submit.click().run()
    assert not test.exception
    assert service.view(paused)["decision"]["action"] == "stop"
    assert "Decision recorded" in texts(open_app(service, "run", id=paused))


def test_unknown_run_and_system_ids_are_handled(tmp_path):
    service = make_service(tmp_path)

    for view, identifier in (("run", "00000000-0000-4000-8000-000000000000"), ("run", "nope"),
                             ("system", "nope"), ("investigate", "../etc")):
        test = open_app(service, view, id=identifier)
        assert not test.exception and test.error, (view, identifier)


def test_application_content_in_evidence_is_never_rendered_as_markup(stored, tmp_path):  # noqa: F811
    area, valid, _ = stored
    copy = copy_area(area, tmp_path / "copy")
    trace_path = copy / valid / "evidence" / "trace.json"
    trace = json.loads(trace_path.read_text(encoding="utf-8"))

    for message in trace[0]["messages"]:
        if message["type"] == "tool":
            content = json.loads(message["content"])
            content["response"]["body"] = '{"note": "![x](http://evil.test/p.png) <script>alert(1)</script>"}'
            message["content"] = json.dumps(content)

    trace_path.write_text(json.dumps(trace), encoding="utf-8")
    outcome_path = copy / valid / "reports" / "outcome.json"
    outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
    outcome["findings"][0]["assessment"] = "See ![beacon](http://evil.test/b.png) and [link](http://evil.test)."
    outcome_path.write_text(json.dumps(outcome), encoding="utf-8")

    test = open_app(make_service(tmp_path, runs=copy), "run", id=valid)
    assert not test.exception
    prose = "\n".join(element.value for element in test.markdown)
    assert "![beacon](" not in prose and "](http://evil.test" not in prose
    assert "\\!\\[beacon\\]" in prose  # Shown literally, not as an image.
    assert any("<script>alert(1)</script>" in code.value for code in test.code)  # In a code block only.


def test_the_interface_refuses_to_run_when_it_is_not_bound_to_loopback():
    """Started with plain `streamlit run` it would listen on every interface; it must stop instead."""
    test = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not test.exception
    assert "only runs on the local machine" in "\n".join(error.value for error in test.error)
    assert not test.tabs and not test.button  # Nothing of the application is shown.
