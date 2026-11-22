import streamlit as st

from testing_agent_app.service.systems import Endpoint, Target
from testing_agent_app.ui.common import (
    EXECUTION_COLORS,
    EXECUTION_LABELS,
    METHODS,
    OUTCOME_COLORS,
    OUTCOME_LABELS,
    badge,
    flash,
    get_service,
    go,
    plain,
    records,
)

service = get_service()
overview = service.overview()

st.title("Systems", icon=":material/dns:")
st.caption("Register a system with its endpoints and business rules, then test one of its endpoints against a rule.")

if not overview:
    st.info("No system is registered yet. Register one below, or load the example.", icon=":material/info:")

for row in overview:
    system = row["system"]
    last = row["last_run"]
    badges = [badge(f"{len(system.endpoints)} endpoints", "blue"),
              badge(f"{row['requirements']} requirements" + (f" · {row['version']}" if row["version"] else ""), "blue"),
              badge(f"{row['runs']} investigations", "gray"),
              badge("managed test sandbox", "green") if system.target.runnable
              else badge("external · registered only", "orange")]

    if last:
        if last["case_outcome"]:
            badges.append(badge("last: " + OUTCOME_LABELS[last["case_outcome"]], OUTCOME_COLORS[last["case_outcome"]]))

        else:
            badges.append(badge("last: " + EXECUTION_LABELS[last["execution"]], EXECUTION_COLORS[last["execution"]]))

    with st.container(border=True):
        text, action = st.columns([8, 1], vertical_alignment="center")
        text.markdown(f"### {plain(system.name)}\n{plain(system.purpose)}\n\n" + " ".join(badges))

        if action.button("Open", key=f"open-system-{system.system_id}", icon=":material/arrow_forward:"):
            go("system", id=system.system_id)

FILE_FORMAT = """\
{
  "name": "Orders",
  "purpose": "What the system does.",
  "target": {"kind": "managed_sandbox"},
  "endpoints": [
    {"method": "GET", "path": "/orders/{order_id}", "purpose": "What this endpoint does."}
  ],
  "requirements": {
    "specification_version": "v1",
    "requirements": [ ... ]
  }
}"""

registered_files = {row["system"].source_detail for row in overview}
available = [name for name in service.example_files() if name not in registered_files]

with st.expander("Register a new system", expanded=not overview, icon=":material/add_circle:"):
    by_hand, from_file = st.tabs(["Enter it here", "Import from a file"])

    with from_file:
        st.caption("A system file describes the system, its endpoints and, optionally, its requirements. "
                   "It is registered exactly like a system you enter yourself, and its page says where it came from.")
        st.markdown("**Example files** in `" + service.example_system.parent.as_posix() + "`")

        if available:
            chosen = st.selectbox("Example file", available, label_visibility="collapsed")

            if st.button("Import selected file", key="load-example", icon=":material/download:"):
                try:
                    record = service.import_example(chosen)

                except (ValueError, OSError) as error:
                    st.error(str(error))

                else:
                    flash(f"Registered “{record.name}” from `{chosen}`.")
                    go("system", id=record.system_id)

        else:
            st.caption("Every example file is already registered, or the folder has none.")

        st.markdown("**Or upload your own file**")
        upload = st.file_uploader("System file (JSON)", type=["json"], key="system-upload")

        if upload is not None and st.button("Register uploaded file", key="register-upload", icon=":material/upload:"):
            try:
                record = service.import_upload(upload.getvalue(), upload.name)

            except (ValueError, OSError) as error:
                st.error(str(error))

            else:
                flash(f"Registered “{record.name}” from `{upload.name}`.")
                go("system", id=record.system_id)

        st.markdown("**Expected format**")
        st.code(FILE_FORMAT, language="json")
        st.caption("`target` is `managed_sandbox` (optionally with a `seed` file name from `sandbox/target_app/seed/`), or `{\"kind\": \"external\", \"base_url\": \"https://…\"}` "
                   "(registered only). `requirements` is optional and must be written inside the file, in the same "
                   "format as the catalogs under `specifications/`; every endpoint a requirement uses must be "
                   "listed in `endpoints`. Do not include secrets: files that contain them are rejected.")

    with by_hand, st.form("register-system"):
        name = st.text_input("System name")
        purpose = st.text_area("What does it do?", height=80)
        kind = st.segmented_control(
            "Where can it be exercised?", ["managed_sandbox", "external"], default="managed_sandbox", required=True,
            format_func={"managed_sandbox": "Managed test sandbox (can be investigated here)",
                         "external": "External sandbox URL (registered only for now)"}.get)
        base_url = st.text_input("Base URL of your sandbox (external only)", placeholder="https://sandbox.example.test")
        st.caption("Endpoints. Only register endpoints of a sandbox that cannot affect a real system: "
                   "keeping them apart from production is your responsibility.")
        table = st.data_editor(
            [{"method": "GET", "path": "", "purpose": ""}], num_rows="dynamic", hide_index=True,
            key="register-endpoints", alt="Endpoints of the new system: method, path and what each does",
            column_config={"method": st.column_config.SelectboxColumn("Method", options=METHODS, required=True),
                           "path": st.column_config.TextColumn("Path", help="For example /orders/{order_id}"),
                           "purpose": st.column_config.TextColumn("What it does")})
        submitted = st.form_submit_button("Register system", type="primary", icon=":material/add:")

    if submitted:
        try:
            created = service.systems.create(
                name, purpose, Target(kind=kind, base_url=base_url.strip() or None),
                [Endpoint(method=r["method"], path=r["path"], purpose=r["purpose"])
                 for r in records(table) if r.get("path")])

        except ValueError as error:
            st.error(str(error))

        else:
            flash(f"Registered “{created.name}”. Add its requirements next.")
            go("system", id=created.system_id)
