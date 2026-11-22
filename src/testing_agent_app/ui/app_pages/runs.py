import streamlit as st

from testing_agent_app.ui.common import get_service, run_rows

service = get_service()
st.title("All investigations")
rows = service.list_runs()

if not rows:
    st.info("There are no investigations yet. Open a system and test one of its endpoints.")

run_rows(rows, show_system=True)
