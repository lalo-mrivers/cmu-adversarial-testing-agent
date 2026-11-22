import streamlit as st

from testing_agent.persistence.status import MILESTONES
from testing_agent_app.ui.common import (
    EXECUTION_COLORS,
    EXECUTION_LABELS,
    STOP_REASON_LABELS,
    badge,
    get_service,
    go,
    param,
    plain,
    progress_line,
    render_calls,
    render_environment,
    render_findings,
    render_plan,
    report_markdown,
    verdict_banner,
    when,
)

service = get_service()
run_id = param("id")

try:
    view = service.view(run_id)

except LookupError as error:
    st.error(str(error))
    st.stop()

context = view["investigation"]
registered = False

if context:
    try:
        service.systems.get(context["system_id"])
        registered = True

    except LookupError:
        pass  # The system was removed; its investigation is kept.

if st.button("Back to the system" if registered else "All investigations", key="back-run",
             icon=":material/arrow_back:", type="tertiary"):
    go("system", id=context["system_id"]) if registered else go("runs")

st.title("Investigation", icon=":material/fact_check:")

if context and not registered:
    st.caption("This system is no longer registered; the investigation and its evidence are kept.")

if context:
    st.markdown(f"**System:** {plain(context['system_name'])} · catalog {plain(context['catalog_version'])}  \n"
                "**Endpoints under test:** "
                + ", ".join(f"`{e['method']} {e['path']}`" for e in context["endpoints_under_test"])
                + "  \n**Requirements:** " + ", ".join(f"`{i}`" for i in context["requirement_ids"]))

    if context.get("extra_focus"):
        st.markdown(f"**Extra detail you added (not part of the catalog):** {plain(context['extra_focus'])}")

st.caption(f"Investigation {run_id} · started {when(view['run']['created_at'])}")

with st.expander("Instruction sent to the Planner"):
    st.code(view["run"]["objective"], language=None, wrap_lines=True)


def review_panel(current: dict) -> None:
    with st.container(border=True):
        st.subheader("Human review")
        review = current["review"]
        pause = review["request"]
        st.markdown(plain(STOP_REASON_LABELS.get(review["reason"], review["reason"])))

        if pause.get("question"):
            st.markdown(f"**Question that needs a decision:** {plain(pause['question'])}")

            for fragment in pause.get("fragments", []):
                st.markdown(f"> `{fragment['requirement_id']}` — {plain(fragment['quote'])}")

            st.caption(plain(f"Applicable conditions: {pause.get('applicable_conditions')}. "
                             f"Exceptions and precedence: {pause.get('exception_and_precedence_checks')}"))

        elif pause.get("details"):
            st.markdown(plain(pause["details"]))

        decision = current["decision"]

        if decision:
            st.markdown(f"**Decision recorded:** {plain(decision['action'])} by {plain(decision['reviewer'])} — "
                        f"{plain(decision['resolution'])}")

            return

        with st.form("decision"):
            reviewer = st.text_input("Reviewer", max_chars=120)
            resolution = st.text_area("Reason for closing", max_chars=1000, height=80)

            if st.form_submit_button("Close case", icon=":material/gavel:"):
                try:
                    service.decide_stop(run_id, reviewer, resolution)

                except ValueError as error:
                    st.error(str(error))

                else:
                    st.rerun()

        st.caption("Continuing the case with a decision that changes the scope or the specification requires "
                   "acceptance checks and is done with `testing-agent review` and `run --review-of`.")


def render(current: dict) -> None:
    verdict_banner(current)
    execution = current["execution"]
    line = f"Status: {badge(EXECUTION_LABELS[execution], EXECUTION_COLORS[execution])}"

    if current["stop_reason"]:
        line += f" {plain(STOP_REASON_LABELS.get(current['stop_reason'], current['stop_reason']))}"

    st.markdown(line)
    st.markdown(progress_line(current))
    status = current["status"] or {}

    if execution == "failed" and status.get("error"):
        st.error(f"Technical error: {status['error']}")

    if execution == "interrupted":
        st.warning("The process ended before this investigation was closed. Its partial evidence is preserved, "
                   "but the outcome is not conclusive.")

    if execution == "running":
        if status.get("stop_requested"):
            st.info("Stop requested: no new operations will be sent. Waiting for the one in progress to finish.")

        else:
            if st.button("Request stop", key="stop", icon=":material/stop:"):
                try:
                    service.request_stop(run_id)

                except LookupError as error:
                    st.error(str(error))

                st.rerun(scope="fragment")

            st.caption("Stopping is polite: the current request finishes, nothing new is sent, and the Verifier "
                       "judges what was already recorded.")

        st.caption("This page refreshes itself every few seconds.")

    if status.get("milestones"):
        with st.expander("Activity log"):
            for item in status["milestones"]:
                detail = f" — {item['detail']}" if item.get("detail") else ""
                st.markdown(plain(f"{item['at'][11:19]}  {MILESTONES.get(item['event'], item['event'])}{detail}"))

    if execution == "pending_review" and current["review"]:
        review_panel(current)

    render_findings(current)
    render_plan(current)
    render_calls(current)
    render_environment(current)

    if execution != "running":
        st.download_button("Download report (Markdown)", report_markdown(current), file_name=f"investigation-{run_id}.md",
                           mime="text/markdown", key="download-report", icon=":material/download:")


running = view["execution"] == "running"


@st.fragment(run_every="3s" if running else None)
def live() -> None:
    current = service.view(run_id)
    render(current)

    if running and current["execution"] != "running":
        st.rerun()


live()
