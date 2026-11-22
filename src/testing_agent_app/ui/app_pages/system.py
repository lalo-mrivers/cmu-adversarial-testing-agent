import streamlit as st

from testing_agent.domain.requirements import OperationReference
from testing_agent_app.service.investigations import Busy
from testing_agent_app.service.systems import Endpoint
from testing_agent_app.ui.common import METHODS, flash, get_service, go, pair, param, plain, run_rows, when

service = get_service()
store = service.systems
system_id = param("id")

try:
    record = store.get(system_id)

except LookupError as error:
    st.error(str(error))

    if st.button("Back to systems", icon=":material/arrow_back:"):
        go("systems")

    st.stop()

if st.button("Systems", key="back-systems", icon=":material/arrow_back:", type="tertiary"):
    go("systems")

st.title(plain(record.name))
st.markdown(plain(record.purpose))
origin = f"imported from `{record.source_detail}`" if record.source == "imported" else "entered by hand"
target = ("managed test sandbox" if record.target.runnable
          else f"external sandbox {record.target.base_url}, registered only; running it needs an operational review")
st.caption(f"Registered {when(record.created_at)}, {origin}. Target: {target}.")

versions = store.versions(system_id)
latest = store.catalog(system_id) if versions else None
endpoints_tab, requirements_tab, runs_tab = st.tabs(["Endpoints", "Requirements", "Investigations"])

with endpoints_tab:
    st.caption("Pick an endpoint to test it against one of the system's requirements.")

    for endpoint in record.endpoints:
        pairs = {(endpoint.method, endpoint.path)}
        covering = [rule.id for rule in latest.requirements
                    if pairs & {(op.method, op.path) for op in rule.operations}] if latest else []
        coverage = f"Covered by {', '.join(covering)}" if covering else "Not covered by any requirement yet"

        with st.container(border=True):
            text, action = st.columns([8, 2], vertical_alignment="center")
            text.markdown(f"`{endpoint.method} {endpoint.path}`  \n{plain(endpoint.purpose)}  \n:gray[{coverage}]")

            if action.button("Test this endpoint", key=f"test-{endpoint.key}", icon=":material/science:"):
                go("investigate", id=system_id, endpoint=endpoint.key)

    with st.expander("Add an endpoint"):
        with st.form("add-endpoint", clear_on_submit=True):
            method = st.selectbox("Method", METHODS)
            path = st.text_input("Path", placeholder="/orders/{order_id}")
            purpose = st.text_input("What it does")

            if st.form_submit_button("Add endpoint", icon=":material/add:"):
                try:
                    store.add_endpoint(system_id, Endpoint(method=method, path=path, purpose=purpose))

                except ValueError as error:
                    st.error(str(error))

                else:
                    flash(f"Added {method} {path}.")
                    go("system", id=system_id)

with requirements_tab:
    if versions:
        version = st.selectbox("Catalog version", versions[::-1], key=f"version-{system_id}",
                               help="Requirements are never edited: adding one saves a new version.")

        for rule in store.catalog(system_id, version).requirements:
            with st.expander(f"{rule.id} — {rule.description}"):
                st.markdown(f"**Expected behavior.** {plain(rule.expected_behavior)}")
                st.markdown("**Endpoints.** " + ", ".join(f"`{op.method} {op.path}`" for op in rule.operations))

                for item in rule.preconditions:
                    st.markdown(f"- Precondition: {plain(item)}")

                for item in rule.exceptions:
                    st.markdown(f"- Exception {plain(item.id)}: when {plain(item.condition)}, "
                                f"then {plain(item.expected_behavior)}")

    else:
        st.info("This system has no requirements yet. Add the first one below.")

    with st.expander("Add a requirement", expanded=not versions):
        with st.form("add-requirement", clear_on_submit=True):
            number = (len(latest.requirements) + 1) if latest else 1
            requirement_id = st.text_input("ID", value=f"REQ-{number:03d}")
            description = st.text_input("Short description", placeholder="The amount of an approved payment cannot change.")
            chosen = st.multiselect("Endpoints this rule applies to", [e.key for e in record.endpoints])
            behavior = st.text_area("Expected behavior", height=100,
                                    help="What the application must do when the rule applies, in plain words.")
            preconditions = st.text_area("Preconditions (one per line, optional)", height=70)

            if st.form_submit_button("Save requirement", type="primary", icon=":material/save:"):
                try:
                    saved = store.add_requirement(
                        system_id, requirement_id=requirement_id.strip(), description=description,
                        operations=[OperationReference(method=m, path=p) for m, p in map(pair, chosen)],
                        expected_behavior=behavior,
                        preconditions=[line.strip() for line in preconditions.splitlines() if line.strip()])

                except ValueError as error:
                    st.error(str(error))

                else:
                    flash(f"Saved as {saved}. Earlier versions are unchanged.")
                    go("system", id=system_id)

runs = service.list_runs(system_id)

with runs_tab:
    if st.button("New investigation", type="primary", key="new-investigation", icon=":material/science:"):
        go("investigate", id=system_id)

    if runs:
        run_rows(runs, show_system=False)

    else:
        st.caption("No investigation has been run for this system yet.")

with st.expander("Remove this system (test tool)", icon=":material/delete:"):
    st.caption("Removes the registration and all its requirement versions so it can be registered again, for "
               "example from the example file. Stored investigations are kept unless you tick the box; if you "
               "register the same system again, the kept ones are listed under it again.")
    also_runs = st.checkbox(f"Also delete its {len(runs)} stored investigation(s) and their evidence",
                            disabled=not runs, key=f"remove-runs-{system_id}")
    sure = st.checkbox("I understand this cannot be undone", key=f"remove-sure-{system_id}")

    if st.button("Remove system", disabled=not sure, key="remove-system", icon=":material/delete:"):
        try:
            deleted = service.delete_system(system_id, delete_runs=also_runs)

        except (Busy, OSError) as error:
            st.error(str(error))

        else:
            kept = "" if also_runs else " Its stored investigations were kept."
            flash(f"Removed “{record.name}”." + (f" Deleted {deleted} investigation(s)." if deleted else "") + kept)
            go("systems")
