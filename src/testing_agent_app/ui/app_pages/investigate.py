import streamlit as st

from testing_agent_app.service.investigations import Busy
from testing_agent_app.ui.common import get_service, go, pair, param, plain

service = get_service()
system_id = param("id")

try:
    record = service.systems.get(system_id)

except LookupError as error:
    st.error(str(error))
    st.stop()

if st.button("Back to the system", key="back-system", icon=":material/arrow_back:", type="tertiary"):
    go("system", id=system_id)

st.title(f"New investigation · {record.name}")

if not record.target.runnable:
    st.warning("This system's target is external. Running against it needs an operational review and is "
               "not available from this application. You can still register its endpoints and requirements.")

versions = service.systems.versions(system_id)

if not versions:
    st.info("Add at least one requirement to this system before investigating it.")
    st.stop()

preselected = param("endpoint")
options = [e.key for e in record.endpoints]
purposes = {e.key: e.purpose for e in record.endpoints}

st.subheader("1 · Which endpoint do you want to test?")
tested = st.multiselect(
    "Endpoint(s) under test", options, default=[preselected] if preselected in options else [],
    format_func=lambda key: f"{key} — {purposes[key]}", key=f"inv-endpoints-{system_id}-{preselected}")

st.subheader("2 · Which requirement should it be verified against?")
version = st.selectbox("Catalog version", versions[::-1], key=f"inv-version-{system_id}")
catalog = service.systems.catalog(system_id, version)
tested_pairs = {pair(key) for key in tested}
applicable = [rule for rule in catalog.requirements
              if tested_pairs & {(op.method, op.path) for op in rule.operations}]
# Pre-select the rules the chosen endpoints can verify on their own.
self_contained = [rule.id for rule in applicable
                  if {(op.method, op.path) for op in rule.operations} <= tested_pairs]
descriptions = {rule.id: rule.description for rule in applicable}
chosen = st.multiselect(
    "Requirement(s)", [rule.id for rule in applicable], default=self_contained,
    format_func=lambda rule_id: f"{rule_id} — {descriptions[rule_id]}",
    key=f"inv-req-{system_id}-{version}-{'|'.join(sorted(tested))}")

if tested and not applicable:
    st.caption("No requirement of this version covers the chosen endpoint(s). Add one on the system page.")

st.subheader("3 · Anything extra to look for? (optional)")
focus = st.text_area(
    "Extra requirement or detail", height=90, key=f"inv-focus-{system_id}",
    placeholder="For example: also check what happens if the amount is changed after approval.",
    help="Sent to the Planner as extra focus and labelled as yours. The Verifier can only cite requirements "
         "registered in the catalog; to make this a formal requirement, add it on the system page.")

st.subheader("4 · Setup operations (optional)")
setup = st.multiselect(
    "Other endpoints the agents may use to prepare the scenario", [k for k in options if k not in tested],
    format_func=lambda key: f"{key} — {purposes[key]}", key=f"inv-setup-{system_id}-{'|'.join(sorted(tested))}",
    help="For example, creating a reservation before cancelling it. Anything not listed here stays forbidden.")

validation = service.validate(system_id, version, [pair(k) for k in tested], chosen, [pair(k) for k in setup], focus)
st.subheader("Check and run")

if validation.errors:
    st.warning("\n".join(f"- {plain(error)}" for error in validation.errors), title="Before you can run")

else:
    st.markdown("**The agents may use:** " + ", ".join(f"`{op.method} {op.path}`" for op in validation.operations))

    with st.expander("Instruction sent to the Planner"):
        st.caption("Built from your selections and the catalog text; nothing else is added.")
        st.code(validation.objective, language=None, wrap_lines=True)

    limits = validation.limits
    st.caption(f"Limits per run: up to {limits['max_tool_calls']} requests and {limits['timeout_seconds']} seconds.")

active = service.active_run_id

if active:
    st.info("Another investigation is running; wait for it or stop it first.")

    if st.button("Open the running investigation", key="open-active"):
        go("run", id=active)

if st.button("Run investigation", type="primary", icon=":material/play_arrow:", key="run",
             disabled=not validation.ok or bool(active)):
    try:
        run_id = service.start(system_id, version, [pair(k) for k in tested], chosen, [pair(k) for k in setup], focus)

    except (Busy, ValueError) as error:
        st.error(str(error))

    else:
        go("run", id=run_id)
