import streamlit as st

from testing_agent_app.ui.common import OUTCOME_COLORS, OUTCOME_HELP, OUTCOME_LABELS, badge, get_service, go, run_rows

service = get_service()
overview = service.overview()
runs = service.list_runs()

with st.container(border=True):
    st.title("Does your application really follow its business rules?")
    st.markdown(
        "A rule such as “the amount of an approved payment cannot change” is easy to write down and easy to "
        "break by accident. Proving it holds means thinking like an attacker: which sequence of valid calls "
        "could bend it? This tool does that thinking with three AI agents, inside limits you set, against a "
        "private test copy of your application, and keeps the evidence.")

    with st.container(horizontal=True):
        if st.button("Open systems", type="primary", icon=":material/dns:", key="home-systems"):
            go("systems")

        if st.button("See investigations", icon=":material/history:", key="home-runs"):
            go("runs")

with st.container(horizontal=True):
    st.metric("Systems registered", len(overview), border=True, height="stretch")
    st.metric("Requirements", sum(row["requirements"] for row in overview), border=True, height="stretch")
    st.metric("Investigations", len(runs), border=True, height="stretch")

st.subheader("How you use it", icon=":material/route:")
steps = [
    (":material/dns:", "1 · Register", "Describe a system: its endpoints and the business rules they must obey. "
     "Rules are saved as versions and never edited in place."),
    (":material/ads_click:", "2 · Choose", "Pick an endpoint and the rule to verify. Add your own detail if you "
     "want the agents to look somewhere specific."),
    (":material/science:", "3 · Investigate", "The agents plan, run and check an experiment. You can stop it at "
     "any time."),
    (":material/fact_check:", "4 · Read the report", "A verdict backed by recorded responses, with its limits. "
     "Everything is stored."),
]

for column, (icon, title, text) in zip(st.columns(len(steps)), steps):
    with column.container(border=True, height="stretch"):
        st.markdown(f"{icon} **{title}**")
        st.caption(text)

st.subheader("How the agents work", icon=":material/smart_toy:")
agents = [
    ("Planner", "plans", "Turns the rule and your question into a short experiment: a hypothesis and the exact "
     "requests worth trying."),
    ("Executor", "runs", "Carries out the plan by sending requests to the test application. It can only use the "
     "operations you authorize, within call and time limits."),
    ("Verifier", "checks", "Reads what was really recorded, compares it with the rule and issues the verdict. "
     "Every claim must cite a recorded response."),
]

for column, (name, role, text) in zip(st.columns(len(agents)), agents):
    with column.container(border=True, height="stretch"):
        st.markdown(f"**{name}** {badge(role, 'blue')}")
        st.caption(text)

st.subheader("Safe by design", icon=":material/verified_user:")
st.markdown(
    "- :material/check_circle: The target is a private copy of an application with synthetic data. Real systems "
    "are never contacted.\n"
    "- :material/check_circle: Only the operations you authorize can be used, and every run has call and time "
    "limits.\n"
    "- :material/check_circle: A plan is only an intention. Results come from recorded responses, never from what "
    "a model says it did.\n"
    "- :material/check_circle: If the evidence is not enough, the answer is “inconclusive”. The agents are not "
    "pushed toward a favorable result.")

st.subheader("What a verdict means", icon=":material/gavel:")

for key in ("violation", "no_violation_observed", "inconclusive"):
    with st.container(horizontal=True, vertical_alignment="center"):
        st.markdown(badge(OUTCOME_LABELS[key], OUTCOME_COLORS[key]), width=340)
        st.caption(OUTCOME_HELP[key])

st.subheader("Recent investigations", icon=":material/history:")

if runs:
    run_rows(runs[:3], show_system=True)

    if len(runs) > 3 and st.button("All investigations", key="home-all-runs", icon=":material/arrow_forward:"):
        go("runs")

else:
    st.caption("There are no investigations yet. Register a system and test one of its endpoints.")
