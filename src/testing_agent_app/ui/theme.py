THEME = {
    "baseRadius": "8px",
    "paddingTop": "2rem",
    "light.primaryColor": "#1f5fbf",
    "light.linkColor": "#1f5fbf",
    "light.backgroundColor": "#ffffff",
    "light.secondaryBackgroundColor": "#f3f5f8",
    "light.textColor": "#1b1f24",
    "light.borderColor": "#d7dce2",
    "dark.primaryColor": "#2563eb",
    "dark.linkColor": "#7fb0ff",
    "dark.backgroundColor": "#14171c",
    "dark.secondaryBackgroundColor": "#1e242c",
    "dark.textColor": "#e6e9ee",
    "dark.borderColor": "#2f3742",
}


def theme_flags() -> list[str]:
    """The theme as `streamlit run` options."""
    flags: list[str] = []

    for name, value in THEME.items():
        flags += [f"--theme.{name}", value]

    return flags
