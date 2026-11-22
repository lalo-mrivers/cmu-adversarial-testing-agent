from setuptools import find_namespace_packages, setup


setup(
    name="adversarial-testing-agent",
    version="0.1.0",
    description="Agent-based investigation of application business rules.",
    python_requires=">=3.12",
    package_dir={"": "src"},
    packages=find_namespace_packages(
        where="src",
        include=["testing_agent", "testing_agent.*", "testing_agent_app", "testing_agent_app.*"],
    ),
    include_package_data=False,
    package_data={
        "testing_agent": ["prompts/*.md"],
    },
    install_requires=[
        "httpx>=0.28,<1",
        "langchain>=1,<2",
        "langchain-core>=1.8.1,<2",
        "langgraph>=1.2.22,<2",
        "langgraph-checkpoint>=4.3,<5",
        "langgraph-checkpoint-sqlite>=3.1,<4",
        "langgraph-prebuilt>=1.1,<2",
        "mcp>=1.29,<2",
        "pydantic>=2.7.4,<3",
    ],
    extras_require={
        "openai": [
            "langchain-openai>=1.8,<2",
        ],
        # Web application (`testing_agent_app`). The library does not need it.
        "app": [
            "streamlit>=1.67,<2",
        ],
    },
    entry_points={
        "console_scripts": [
            "testing-agent=testing_agent.cli:main",
            "testing-agent-app=testing_agent_app.cli:main",
        ],
    },
)