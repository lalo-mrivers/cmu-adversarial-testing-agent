import streamlit as st

from testing_agent_app.ui.common import get_service, header

st.set_page_config(page_title="Adversarial testing agent", page_icon=":material/science:", layout="wide")

page = st.navigation(
    [
        st.Page("app_pages/home.py", title="Home", icon=":material/home:", url_path="home", default=True,
                help="What this tool does, how it works and what its verdicts mean."),
        st.Page("app_pages/systems.py", title="Systems", icon=":material/dns:", url_path="systems",
                help="Registered systems with their endpoints and business rules. Register one, then test an endpoint."),
        st.Page("app_pages/runs.py", title="Investigations", icon=":material/history:", url_path="investigations",
                help="Every stored investigation, newest first, with its status and verdict."),
        st.Page("app_pages/system.py", title="System", icon=":material/hub:", url_path="system", visibility="hidden"),
        st.Page("app_pages/investigate.py", title="New investigation", icon=":material/science:",
                url_path="investigate", visibility="hidden"),
        st.Page("app_pages/run.py", title="Investigation", icon=":material/fact_check:", url_path="run",
                visibility="hidden"),
    ],
    position="top",
)
header(get_service())
page.run()
