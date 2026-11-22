import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import streamlit as st

from testing_agent.persistence.status import MILESTONES
from testing_agent_app.service.investigations import InvestigationService

EXECUTION_LABELS = {
    "running": "Running", "closed": "Closed", "stopped": "Stopped", "pending_review": "Pending review",
    "failed": "Failed", "interrupted": "Interrupted",
}
EXECUTION_COLORS = {
    "running": "blue", "closed": "gray", "stopped": "orange", "pending_review": "orange",
    "failed": "red", "interrupted": "violet",
}
OUTCOME_LABELS = {
    "violation": "Violation observed",
    "no_violation_observed": "No violation observed within the tested scope",
    "inconclusive": "Inconclusive",
}
OUTCOME_COLORS = {"violation": "red", "no_violation_observed": "green", "inconclusive": "orange"}
OUTCOME_HELP = {
    "violation": "The application behaved differently from what the requirement says, and the recorded "
                 "responses show it.",
    "no_violation_observed": "In the experiments that were run, the application behaved as the requirement "
                             "says. This does not prove the rule holds everywhere, only within what was tested.",
    "inconclusive": "The evidence was not enough to confirm or rule out a violation. The limitations say "
                    "what is missing.",
}
STOP_REASON_LABELS = {
    "user_stopped": "Stopped at the user's request.",
    "iteration_limit": "The maximum number of reviews was reached.",
    "tool_limit": "The tool-call limit was exhausted.",
    "timeout": "The investigation ran out of time.",
    "interpretation_review": "The applicable requirements need a human interpretation decision.",
    "scope_change": "The request exceeds the authorized scope.",
    "validation_failed": "The continuation validation did not pass.",
    "startup_unverified": "The test target could not be verified.",
    "containment_lost": "Isolation of the target could not be confirmed.",
    "recovery_required": "An operation may have modified the inventory and its effect is uncertain.",
}
PROGRESS = [
    ("run_started", "Started"), ("target_ready", "Test app ready"), ("plan_ready", "Experiment planned"),
    ("execution_recorded", "Experiment executed"), ("evaluation_done", "Evidence checked"),
]

_PUNCTUATION = re.compile(r"([!-/:-@\[-`{-~])")


def plain(value: Any) -> str:
    """Markdown that renders `value` literally."""

    return _PUNCTUATION.sub(r"\\\1", str(value)).replace("\n", "  \n")


def badge(text: str, color: str) -> str:
    return f":{color}-badge[{plain(text)}]"


def when(timestamp: str) -> str:
    return timestamp[:19].replace("T", " ") + " UTC"


# ---- service and navigation ------------------------------------------------------
def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--working-directory", type=Path, default=Path("."))
    parser.add_argument("--settings", type=Path, default=Path("config/application.json"))
    parser.add_argument("--runs-path", type=Path, default=Path("var/runs"))
    parser.add_argument("--checkpoint-path", type=Path, default=Path("var/checkpoints/checkpoints.sqlite"))
    parser.add_argument("--systems-path", type=Path, default=Path("var/systems"))

    return parser.parse_known_args(sys.argv[1:])[0]


@st.cache_resource(show_spinner=False)
def _shared_service(directory: str, settings: str, runs: str, checkpoints: str, systems: str) -> InvestigationService:
    """One service per server process: it owns the single active investigation."""

    return InvestigationService(
        directory, settings=Path(settings), runs_path=Path(runs),
        checkpoint_path=Path(checkpoints), systems_path=Path(systems),
    )


LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def get_service() -> InvestigationService:
    injected = st.session_state.get("_service")  # Tests inject their own.

    if injected is not None:
        return injected

    if st.get_option("server.address") not in LOOPBACK:
        # The interface has no authentication and can start investigations: it is for this machine only.
        st.error("This interface has no sign-in, so it only runs on the local machine. "
                 "Start it with `testing-agent-app`, which listens on 127.0.0.1 only.", title="Not started on loopback")
        st.stop()

    args = _arguments()

    return _shared_service(str(args.working_directory), str(args.settings), str(args.runs_path),
                           str(args.checkpoint_path), str(args.systems_path))


METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
# Page files, relative to the entrypoint (app.py). The detail pages are reached by buttons, not the menu.
PAGES = {
    "home": "app_pages/home.py", "systems": "app_pages/systems.py", "runs": "app_pages/runs.py", "system": "app_pages/system.py",
    "investigate": "app_pages/investigate.py", "run": "app_pages/run.py",
}


def go(view: str, **params: str) -> None:
    """Open a page of this application. Parameters travel in the address, so a page can be bookmarked."""
    st.switch_page(PAGES[view], query_params=params)


def param(name: str) -> str:
    return str(st.query_params.get(name, ""))


def pair(key: str) -> tuple[str, str]:
    """Split "METHOD /path" into its two parts."""
    method, _, path = key.partition(" ")

    return method, path


def records(value) -> list[dict[str, str]]:
    """Rows of a data editor as plain dictionaries of stripped text."""
    rows = value.to_dict("records") if hasattr(value, "to_dict") else list(value)

    return [{k: ("" if v is None or v != v else str(v).strip()) for k, v in row.items()} for row in rows]


def flash(message: str) -> None:
    """Show a message once, on the next page that is displayed."""
    st.session_state["_flash"] = message


def header(service: InvestigationService) -> None:
    active = service.active_run_id

    if active:
        with st.container(horizontal=True, vertical_alignment="center"):
            st.caption("An investigation is running.")

            if st.button("Open it", key="open-active-run", icon=":material/open_in_new:"):
                go("run", id=active)

    message = st.session_state.pop("_flash", None)

    if message:
        st.success(message, icon=":material/check_circle:")


# ---- rendering ----------------------------------------------------------------------
def investigation_summary(run: dict[str, Any]) -> str:
    """One line saying what a stored investigation was about."""
    context = run.get("investigation")

    if not context:
        return "Started from the command line"

    endpoints = ", ".join(f"{e['method']} {e['path']}" for e in context["endpoints_under_test"])

    return f"{endpoints} → {', '.join(context['requirement_ids'])}"


def run_rows(runs: list[dict[str, Any]], *, show_system: bool) -> None:
    """A list of investigations with their status, verdict and a button to open each."""

    for run in runs:
        with st.container(border=True):
            text, status, action = st.columns([6, 3, 1], vertical_alignment="center")
            context = run.get("investigation") or {}
            prefix = f"**{plain(context['system_name'])}** · " if show_system and context else ""
            text.markdown(f"{prefix}{plain(investigation_summary(run))}  \n:gray[{when(run['created_at'])}]")
            line = badge(EXECUTION_LABELS[run["execution"]], EXECUTION_COLORS[run["execution"]])

            if run["case_outcome"]:
                line += " " + badge(OUTCOME_LABELS[run["case_outcome"]], OUTCOME_COLORS[run["case_outcome"]])

            status.markdown(line)

            if action.button("Open", key=f"open-{run['run_id']}", icon=":material/open_in_new:"):
                go("run", id=run["run_id"])


def verdict_banner(view: dict[str, Any]) -> None:
    outcome, execution = view["case_outcome"], view["execution"]

    if execution == "pending_review":
        # A paused run also carries an "inconclusive" outcome, but what matters now is the decision it needs.
        st.warning("The investigation paused on purpose; see the review request below.",
                   title="A human decision is needed", icon=":material/gavel:")

    elif outcome:
        show, icon = {"violation": (st.error, ":material/error:"),
                      "no_violation_observed": (st.success, ":material/check_circle:"),
                      "inconclusive": (st.warning, ":material/help:")}[outcome]
        show(OUTCOME_HELP[outcome], title=f"Verdict: {OUTCOME_LABELS[outcome]}", icon=icon)

    elif execution == "running":
        st.info("The verdict appears here when the Verifier finishes.", title="In progress", icon=":material/hourglass_top:")

    elif execution == "failed":
        st.error("It ended with a technical error, so there is no verdict.", title="The investigation failed")

    else:
        st.warning("The investigation did not finish; any recorded evidence supports no verdict.", title="No verdict")


def progress_line(view: dict[str, Any]) -> str:
    done = {item["event"] for item in (view["status"] or {}).get("milestones", [])}
    parts = [f"{':material/check_circle:' if event in done else ':material/radio_button_unchecked:'} {label}"
             for event, label in PROGRESS]

    if "plan_blocked" in done:
        parts.append(":material/block: Plan blocked")

    return " → ".join(parts)


def _call_title(ref: dict[str, Any]) -> str:
    if not ref["resolved"]:
        return "Citation not resolved in the trace"

    call = ref["call"]
    path = f" ({call['resolved_path']})" if call["resolved_path"] else ""

    return f"{call['arguments'].get('method')} {call['arguments'].get('path')}{path} → HTTP {call['status_code']}"


def render_findings(view: dict[str, Any]) -> None:
    st.subheader("Findings by requirement")
    st.caption("Each finding is one business rule from the catalog, judged only on the recorded evidence.")

    if view["evaluation"]:
        st.markdown(plain(view["evaluation"]))

    if not view["findings"]:
        blocker = f": the plan was blocked — {view['planning_blocker']}" if view["planning_blocker"] else ""
        st.markdown(plain(f"This investigation produced no findings{blocker}."))

    for finding in view["findings"]:
        with st.container(border=True):
            rule = finding["requirement"]
            title = f"**{plain(finding['requirement_id'])}**" + (f" — {plain(rule['description'])}" if rule else "")
            st.markdown(f"{title}  \n{badge(OUTCOME_LABELS[finding['outcome']], OUTCOME_COLORS[finding['outcome']])}")
            st.markdown(f"**What was checked.** {plain(finding['scope'])}")

            if rule:
                version = finding.get("specification_version") or view["run"]["specification_version"]
                st.markdown(f"**Expected behavior (catalog {plain(version)}).** {plain(rule['expected_behavior'])}")

            st.markdown(f"**Verifier's reasoning.** {plain(finding['assessment'])}")

            if finding["limitations"]:
                st.markdown("**Limitations**\n\n" + "\n".join(f"- {plain(item)}" for item in finding["limitations"]))

            for index, ref in enumerate(finding["references"]):
                with st.expander(_call_title(ref) if ref["resolved"] else ":material/warning: " + _call_title(ref)):
                    st.caption(f"Record {ref['evidence_id']} · call {ref['tool_call_id']}")

                    if ref["resolved"]:
                        st.markdown("**Request**")
                        st.code(json.dumps(ref["call"]["arguments"], indent=2, ensure_ascii=False, default=str),
                                language="json", wrap_lines=True)
                        st.markdown("**Application response**")
                        st.code(ref["call"]["response_body"] or "(empty)", language="json", wrap_lines=True)

                    else:
                        st.warning("The stored trace has no call with a response for this citation; "
                                   "it must not be taken as an observation.")


def render_plan(view: dict[str, Any]) -> None:
    experiment = view["experiment"]

    if not experiment:
        return

    st.subheader("Experiment plan")
    st.caption("This is the Planner's intent; it does not prove anything was executed. "
               "What was executed is under “What was actually sent”.")
    st.markdown(f"**Hypothesis.** {plain(experiment['hypothesis'])}")

    for item in experiment.get("preconditions", []):
        st.markdown(f"- **Precondition {plain(item['requirement_id'])}:** {plain(item['condition'])}  \n"
                    f"  :gray[Proposed verification: {plain(item['verification'])}]")

    for number, step in enumerate(experiment["steps"], 1):
        operation = step.get("operation")
        target = f"`{operation['method']} {operation['path']}`" if operation else "*no application request*"
        st.markdown(f"{number}. **{plain(step['id'])}** {target} — {plain(step['instruction'])}  \n"
                    f"   :gray[Expected: {plain(step['expected_observation'])}]")


def render_calls(view: dict[str, Any]) -> None:
    st.subheader("What was actually sent")
    st.caption("The requests the Executor made to the test application, with the safety controller's "
               "decision and the HTTP status returned.")
    calls = [call for call in view["calls"] if call["tool_call_id"]]

    if not calls:
        st.markdown("No call was recorded.")

    else:
        st.dataframe(
            [{"#": i, "Operation": (f"{c['arguments'].get('method')} {c['arguments'].get('path')}"
                                    if c["tool"] == "request_application" else c["tool"]),
              "Controller decision": (c["decision"] or "none recorded") + (f" — {c['reason']}" if c["reason"] else ""),
              "HTTP": c["status_code"] or "—"} for i, c in enumerate(calls, 1)],
            hide_index=True, alt="Requests sent to the application, with the controller's decision and HTTP status")

        with st.expander("Arguments of each call"):
            for i, call in enumerate(calls, 1):
                st.caption(f"Call {i}")
                st.code(json.dumps(call["arguments"], indent=2, ensure_ascii=False, default=str),
                        language="json", wrap_lines=True)

    for call in view["calls"]:
        if not call["tool_call_id"]:
            st.caption(f"The record ended with: {call['stop_reason']}")


def render_environment(view: dict[str, Any]) -> None:
    context = view["execution_context"]

    if not context:
        return

    st.subheader("Test environment")

    if context.get("external_modifications_excluded") is False:
        st.info("External changes were not excluded during the investigation. The instance identifier and the "
                "initial snapshot do not prove a reset or exclusive isolation.")

    st.markdown(f"- Instance: `{context['instance_id']}`\n- Target: {plain(context['mode'])}, identity "
                f"{plain(context['account'])}\n- Started: {plain(context['initialized_at'])}")


# ---- downloadable report ---------------------------------------------------------------
def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)

    return f"{ticks}\n{text}\n{ticks}"


def report_markdown(view: dict[str, Any]) -> str:
    """A self-contained Markdown report of one investigation (no recommendations yet)."""
    run, context = view["run"], view["investigation"] or {}
    lines = ["# Investigation report", "", f"- Investigation: `{run['run_id']}`", f"- Date: {when(run['created_at'])}"]

    if context:
        lines += [f"- System: {context['system_name']} (`{context['system_id']}`)",
                  f"- Catalog version: `{context['catalog_version']}`",
                  "- Endpoints under test: " + ", ".join(f"`{e['method']} {e['path']}`" for e in context["endpoints_under_test"]),
                  "- Requirements: " + ", ".join(f"`{i}`" for i in context["requirement_ids"]),
                  "- Authorized operations: " + ", ".join(f"`{o['method']} {o['path']}`" for o in context["authorized_operations"])]

        if context.get("extra_focus"):
            lines.append(f"- Extra detail added by the user (not part of the catalog): {context['extra_focus']}")

    lines.append(f"- Execution: {EXECUTION_LABELS[view['execution']]}")

    if view["stop_reason"]:
        lines.append(f"- Stop reason: {STOP_REASON_LABELS.get(view['stop_reason'], view['stop_reason'])}")

    if view["case_outcome"]:
        lines += ["", f"## Verdict: {OUTCOME_LABELS[view['case_outcome']]}", "", OUTCOME_HELP[view["case_outcome"]]]

    if view["evaluation"]:
        lines += ["", view["evaluation"]]

    lines += ["", "## Findings"]

    for finding in view["findings"]:
        rule = finding["requirement"]
        lines += ["", f"### {finding['requirement_id']}" + (f" — {rule['description']}" if rule else ""),
                  f"Outcome: **{OUTCOME_LABELS[finding['outcome']]}**", "", f"**What was checked.** {finding['scope']}"]

        if rule:
            lines += ["", f"**Expected behavior.** {rule['expected_behavior']}"]

        lines += ["", f"**Verifier's reasoning.** {finding['assessment']}"]

        if finding["limitations"]:
            lines += ["", "**Limitations**", *[f"- {item}" for item in finding["limitations"]]]

        for ref in finding["references"]:
            lines += ["", f"Evidence — record `{ref['evidence_id']}`, call `{ref['tool_call_id']}`: " + _call_title(ref)]

            if ref["resolved"]:
                lines += ["", _fence(ref["call"]["response_body"] or "(empty)")]

    if not view["findings"]:
        lines += ["", "This investigation produced no findings."]

    lines += ["", "## What was actually sent"]

    for number, call in enumerate((c for c in view["calls"] if c["tool_call_id"]), 1):
        name = (f"{call['arguments'].get('method')} {call['arguments'].get('path')}"
                if call["tool"] == "request_application" else call["tool"])
        lines.append(f"{number}. `{name}` — {call['decision'] or 'no decision recorded'} — HTTP {call['status_code'] or '—'}")

    environment = view["execution_context"]

    if environment and environment.get("external_modifications_excluded") is False:
        lines += ["", "> External changes were not excluded during the investigation. The instance identifier and "
                      "the initial snapshot do not prove a reset or exclusive isolation."]

    return "\n".join(lines) + "\n"
