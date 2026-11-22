# Autonomous Adversarial Testing Agent for Enterprise Web Applications

A three-agent system that investigates whether a web application complies with its business rules. It designs experiments from a versioned requirement catalog, executes only authorized operations against an isolated instance of the application, and reports only what its recorded evidence supports.

Capstone project for the Agentic AI Program: Building Autonomous Systems for Real-World Applications (Carnegie Mellon University). The application under test in this repository is a **synthetic** inventory and reservations service built as a test bed; no production system or real data is involved.

**Contents:** [Problem](#problem) · [What the agent produces](#what-the-agent-produces) · [Architecture](#architecture) · [Repository layout](#repository-layout) · [Setup](#setup) · [Usage](#usage) · [Outputs](#outputs) · [Evaluation results](#evaluation-results) · [Reviewing the implementation](#reviewing-the-implementation) · [Scope and limitations](#scope-and-limitations)

---

## Problem

Enterprise systems are governed by business rules: which inputs are acceptable, which state transitions are valid, what must still hold after an operation is repeated or combined with others. Verifying that a system complies with those rules is still largely manual. Test suites encode the sequences their authors anticipated, and scanners mostly judge one request at a time. Many violations only appear when operations are combined, repeated or executed from an unexpected state.

A language model alone can propose test ideas, but it cannot execute them or observe what the system actually does, so it cannot tell a hypothetical defect from a real one. This project builds an agent that plans experiments from the rules, acts on the system within an explicit scope, and adapts to what it observes.

The intended users are QA engineers, developers and application-security specialists who need verifiable evidence rather than plausible test ideas.

## What the agent produces

Given an objective, a versioned requirement catalog, a specification version and a set of authorized operations, the agent assigns each requirement it investigates one of three outcomes:

| Outcome | Meaning |
|---|---|
| `violation` | The observations contradict the requirement and its preconditions are established. Must cite recorded calls that received a response. |
| `no_violation_observed` | The checks in the stated scope agree with the requirement. Must cite recorded calls. It is **not** a certificate that the application is free of defects. |
| `inconclusive` | Evidence or a precondition is missing. Must state what is missing. A valid result, not a failure. |

Every citation is a pair `(evidence_id, tool_call_id)` that points to a call and its matching response in the persisted trace.

## Architecture

![Architecture: Requirements and authorised operations feed the Planner; the Planner sends an experiment to the Executor, which calls the Application through MCP tools and passes evidence to the Verifier. The Verifier can revise the hypothesis with the Planner, request another observation from the Executor, or end with findings or a human review. All three agents run inside the LangGraph controller (state, budgets, routing).](architecture.png)

The controller is software, not a fourth agent. It holds the shared state, enforces the budgets and accepts or rejects the route the Verifier proposes: request another observation from the Executor, revise the hypothesis with the Planner, close the case with findings, or pause it for human review. The Executor reaches the application only through the MCP tools.

| Component | Responsibility | Code |
|---|---|---|
| **Planner** | Proposes a hypothesis and a typed experiment (requirement IDs, preconditions, ordered steps with dependencies, expected observations, stop conditions). The plan is validated against the catalog and the authorized operations before it receives a UUID. If no valid plan exists, it returns an explicit `planning_blocker`. | [agents/planner.py](src/testing_agent/agents/planner.py) |
| **Executor** | Runs a ReAct loop (model → one tool call → model) to obtain observations. Resolves identifiers from responses instead of assuming them. Does not decide verdicts. | [agents/executor.py](src/testing_agent/agents/executor.py) |
| **Verifier** | Compares the accumulated evidence with each requirement, its preconditions and documented exceptions; issues findings and proposes the next step: `executor`, `planner`, `end` or `review`. | [agents/verifier.py](src/testing_agent/agents/verifier.py) |
| **LangGraph controller** | Graph, routing, shared state, call/time/iteration budgets, SQLite checkpoints. | [orchestration/](src/testing_agent/orchestration/) |
| **MCP tools** | Three tools registered explicitly with `server.add_tool` and served over `stdio`: `lookup_requirements` (exact lookup by version and ID, or by method and full path), `request_application` (one HTTP request inside the authorized scope) and `check_response` (status, header or JSON Pointer comparison). | [tools/](src/testing_agent/tools/), [integrations/mcp/](src/testing_agent/integrations/mcp/) |
| **HTTP client** | Fixed origin, no redirects, no automatic retries, `trust_env=False`, response size limit. | [integrations/target/http_client.py](src/testing_agent/integrations/target/http_client.py) |
| **Managed sandbox** | A fresh in-memory instance of the test application per investigation, reached through a private ASGI transport (no network port). Network sockets and subprocesses are blocked in that process; if containment is lost the case pauses. | [tools/sandbox.py](src/testing_agent/tools/sandbox.py) |
| **Persistence** | Run metadata, evidence trace, execution status, review decisions and checkpoints. Credentials are redacted from stored output. | [persistence/](src/testing_agent/persistence/) |
| **Web application** | A local Streamlit client of the library: register systems and requirement catalogs, launch investigations, read reports. It contains no agent, verification or authorization logic. | [src/testing_agent_app/](src/testing_agent_app/) |

**Design choices worth knowing before reading the code**

- **Exact requirement retrieval, no RAG.** Rules that demand exact correspondence are selected by version, ID, or method and path, never by semantic similarity.
- **A plan is intent, not execution.** Expected observations in a plan are never treated as results. Only calls with a recorded response can be cited.
- **Tool errors are not violations.** A transport or tool error is kept as evidence but never counts, by itself, as a defect.
- **Membership is not authorization.** An operation mentioned by a requirement is not authorized unless it is in the explicit scope.
- **Mutations are not retried blindly.** After a timeout on a mutating request the case pauses for review (`recovery_required`) instead of assuming a rollback.
- **Conflicting clauses go to a human.** When two applicable requirements disagree, the case pauses (`pending_review`) without a confirmed finding. A supervisor records a decision and the investigation continues in a new instance.
- **Application content is data.** Responses that contain instructions addressed to the agent are treated as data (a prompt injection case is part of the evaluation).
- **Provider-agnostic models.** Agents are built through a model factory from per-agent JSON profiles ([integrations/llm/factory.py](src/testing_agent/integrations/llm/factory.py)). All reported runs used OpenAI `gpt-6.1-sol` with low reasoning effort; other providers have not been validated with these tools and structured outputs.
- **All agent prompts are in English** and live in [src/testing_agent/prompts/](src/testing_agent/prompts/).

## Repository layout

```text
.
├── src/
│   ├── testing_agent/            # Library: agents, domain, orchestration, MCP tools, persistence, CLI
│   │   ├── agents/               #   planner.py, executor.py, verifier.py
│   │   ├── prompts/              #   planner.md, executor.md, verifier.md
│   │   ├── domain/               #   requirements, experiments, findings (Pydantic v2 contracts)
│   │   ├── orchestration/        #   state, graph, routing, budget policy
│   │   ├── tools/                #   MCP tool functions and the managed sandbox
│   │   ├── integrations/         #   LLM factory, MCP client/server, HTTP client
│   │   ├── persistence/          #   runs, evidence, checkpoints, review, status, redaction
│   │   ├── runner.py             #   one investigation end to end (shared by CLI, app, evaluation)
│   │   └── cli.py                #   testing-agent run | serve | review
│   └── testing_agent_app/        # Web application (Streamlit), a client of the library
├── evaluation/                   # Evaluation harness: 52 labelled cases, seeded defects, scoring
├── sandbox/target_app/           # Synthetic application under test (Starlette, in memory) and seeds
├── specifications/target_app/v1/ # Requirement catalog (6 requirements) and OpenAPI description
├── config/
│   ├── application.json          # Model profiles, agent assignment, execution limits
│   ├── demo.json                 # Same, with larger limits for the demo case
│   └── examples/                 # Importable system definitions for the web application
├── demo/                         # Model-free script for the demo case (python -m demo)
├── tests/unit, tests/integration # Automated suite (no real model needed)
├── DEMO_CASE.md                  # Walk-through of the demo case
├── setup.py, requirements.txt, environment.yml
```

The library never imports the web application; [tests/unit/test_package_boundary.py](tests/unit/test_package_boundary.py) checks it.

## Setup

**Requirements:** Python 3.12, Miniconda or Anaconda, and an OpenAI API key for real investigations. The automated tests, the controlled evaluation and the demo script do not need a key.

1. **Create the environment.** From the repository root:

   ```bash
   conda env create -f environment.yml
   conda activate testing-agent
   ```

   `environment.yml` installs [requirements.txt](requirements.txt), which pins the dependencies and installs this package in editable mode with the `openai` and `app` extras (`pip install -e .[openai,app]`). Streamlit is only needed for the web application.

2. **Add the model credentials.** Credentials are read from a JSON file outside the code, never from environment variables or `.env` files. Create `secrets/openai.json` (the `secrets/` folder is excluded by `.gitignore`):

   ```json
   {"api_key": "sk-..."}
   ```

   [config/application.json](config/application.json) points to it with `"credentials_file": "../secrets/openai.json"`; a relative path is resolved against the configuration file.

3. **Review the configuration** in [config/application.json](config/application.json):

   ```json
   {
       "llm": {
           "default": {
               "provider": "openai",
               "model": "gpt-6.1-sol",
               "credentials_file": "../secrets/openai.json",
               "options": {"use_responses_api": true, "reasoning_effort": "low"}
           }
       },
       "agents": {"planner": "default", "executor": "default", "verifier": "default"},
       "execution": {"max_iterations": 3, "max_tool_calls": 10, "timeout_seconds": 120}
   }
   ```

   `max_iterations` counts Verifier reviews, `max_tool_calls` is shared by the whole investigation, and `timeout_seconds` covers Planner, Executor and Verifier together. Each agent can be assigned a different profile.

## Usage

All commands run from the repository root with the `testing-agent` environment active. Commands marked **(LLM)** call the configured model and cost money; with `gpt-6.1-sol` an investigation costs roughly US$0.10.

### 1. Web application (LLM)

```bash
python -m testing_agent_app --settings config/demo.json
```

It serves on `http://127.0.0.1:8765` (loopback only). The `testing-agent-app` command is equivalent once the package is installed. From the interface you can:

- **Register a system:** *Systems → Register a new system → Import from a file*, and choose one of the files in [config/examples/](config/examples/). A system has a name, a purpose, its endpoints and a versioned requirement catalog.
- **Investigate an endpoint:** open a system, choose an endpoint and a requirement, optionally add a detail to the objective and extra setup operations, and run. Progress (plan ready, execution recorded, evaluation finished) is shown while it runs. One investigation runs at a time.
- **Read the report:** global outcome, findings with their citations and limitations, the plan, and every operation that actually reached the application.

[DEMO_CASE.md](DEMO_CASE.md) walks through a complete investigation in the interface.

### 2. Command line (LLM)

PowerShell (on bash, replace the trailing backtick with `\`):

```powershell
python -m testing_agent.cli run `
  --sandbox `
  --requirements specifications/target_app/v1/requirements.json `
  --version v1 `
  --operation GET "/products/{product_id}" `
  --operation POST "/reservations" `
  --operation GET "/reservations/{reservation_id}" `
  --operation POST "/reservations/{reservation_id}/cancel" `
  --objective "Investigate whether cancelling the same reservation twice restores stock more than once. Record the reservation responses and stock observations around both cancellations."
```

- `--operation METHOD PATH` defines the authorized scope; repeat it for each operation. Nothing outside it reaches the application.
- `--sandbox` starts a private instance of the test application. Add `--seed-file sandbox/target_app/seed/products_cancel_retry.json` to start the variant with a seeded defect (see [DEMO_CASE.md](DEMO_CASE.md)).
- `--base-url` (instead of `--sandbox`) names an external target; such a run pauses for operational review instead of executing.
- `--settings` selects another configuration file.

The command prints a JSON summary (also stored as `reports/outcome.json`): `run_id`, `status` (`closed` or `paused`), `case_outcome`, `stop_reason`, the findings, and the paths of the plan and the evidence trace. A paused case is continued with `python -m testing_agent.cli review --run-id <run_id> --decision-file <decision.json>` followed by `run --review-of <run_id>`.

### 3. Demo script (no LLM)

```bash
python -m demo
```

Replays the demo scenario against the clean and the defective application (real HTTP requests over an in-process ASGI transport), without any model, and shows why a per-response check passes while the stock reveals the defect. Details in [DEMO_CASE.md](DEMO_CASE.md).

### 4. Evaluation harness

```bash
python -m evaluation list                                   # the 52 labelled cases
python -m evaluation run --mode controlled                  # scripted models, no key or cost (~3 min)
python -m evaluation run --mode replay --reps 3 --yes       # (LLM) real Verifier on fixed evidence
python -m evaluation run --mode real --reps 3 --yes         # (LLM) the three agents end to end
python -m evaluation score var/evaluations/<uuid>/report.json   # rescore a saved report, no model calls
```

`replay` and `real` refuse to start without `--yes`. `--case <id>` and `--group <group>` restrict a run. Each run writes `var/evaluations/<uuid>/report.json`.

- `controlled` replaces the three agents with scripted models. It tests the harness and the controller's guardrails, not a model.
- `replay` runs the configured Verifier on evidence that already exposes each defect. It measures judgement only.
- `real` runs Planner, Executor and Verifier against the application with the defect seeded.

Cases are defined in [evaluation/cases.json](evaluation/cases.json) and the seeded defects in [evaluation/mutants.py](evaluation/mutants.py). The expected outcome of every case was written from the requirement text before the agents ran.

### 5. Automated tests (no LLM)

```bash
pip install pytest
python -m pytest -q -o required_plugins= -o addopts= --import-mode=importlib --basetemp var/test-tmp
```

Async tests use `asyncio.run` and `IsolatedAsyncioTestCase`, so `pytest-asyncio` (listed in [setup.cfg](setup.cfg)) is not needed and the override disables that requirement. The suite runs the agents with scripted models and uses the real MCP server, sandbox and SQLite checkpoints; it takes about 5 minutes. The interface tests start a real Streamlit server.

## Outputs

Each investigation writes its artifacts to `var/runs/<run_id>/` (the `var/` folder is local and not versioned):

| File | Content |
|---|---|
| `run.json` | Objective, specification version, run UUID and date. |
| `scope.json`, `requirements.json` | The authorized operations and the catalog used by the run. |
| `execution_context.json` | Instance metadata supplied by the system: `instance_id`, initial state snapshot, transport, limits. |
| `experiment.json` | The current plan (or `null` with a blocker). |
| `evidence/trace.json` | Every Executor record: messages, tool calls and their responses, stop reason. |
| `reports/outcome.json` | Global outcome, status and findings with citations and limitations. |
| `status.json` | Execution status and milestones (read by the web application). |
| `review.json`, `decision.json` | A pause for human review and the supervisor's recorded decision. |

**Sample inputs in the repository:** the requirement catalog [specifications/target_app/v1/requirements.json](specifications/target_app/v1/requirements.json), the seeds in [sandbox/target_app/seed/](sandbox/target_app/seed/), the importable systems in [config/examples/](config/examples/) and the labelled cases in [evaluation/cases.json](evaluation/cases.json).

## Evaluation results

The agent was evaluated on a synthetic Inventory and Reservations application built only as a test bed: four REST operations that share state and six documented requirements. The expected outcome of every case was written from the requirement text before the agents ran.

### Test suite

The suite has **52 cases**:

| Group | Cases | What it checks |
|---|---:|---|
| Seeded defects | 21 | A known defect that the agent should report as a violation |
| Defect-free twins | 21 | The same sequence on the unmodified application; no violation should be reported |
| Conflicting clauses | 3 | Two applicable requirements disagree; the case should pause for human review |
| Out-of-scope proposals | 5 | Forbidden actions, including a prompt injection; nothing should reach the application |
| Insufficient evidence | 2 | The agent should stay inconclusive and explain what is missing |

Of these, **47 cases evaluate the three agents end to end**. The other **5 test the controller directly**: four forbidden actions and one budget exhaustion are proposed by scripted agents, because a model rarely produces them on its own.

### Results

The 47 agent cases were run three times with `gpt-6.1-sol`. A case whose application instance did not start counts as a failure.

| Metric | Run 1 | Run 2 | Run 3 | Mean |
|---|---:|---:|---:|---:|
| Recall (defect cases reported as violation) | 57% (12/21) | 48% (10/21) | 52% (11/21) | **52%** |
| Precision (reported violations that were real) | 100% | 100% | 100% | **100%** |
| Defects flagged (violation, or inconclusive citing the symptom) | 19/21 | 19/21 | 19/21 | 90% |
| False violations in defect-free cases | 0/26 | 0/26 | 0/26 | 0 |
| Conflicting cases paused for review | 3/3 | 3/3 | 3/3 | 3/3 |
| Out-of-scope requests reaching the application | 0 | 0 | 0 | 0 |

No out-of-scope request reached the application in the five controller cases either.

**Verifier only.** In three additional runs, the Verifier judged fixed evidence that already exposed each defect. These runs predate the 21st defect, so they cover 20. Recall was 75%, 75% and 80% (mean 77%), and precision stayed at 100%.

### Interpretation

- **The agent did not invent defects.** Precision was 100% in every run, with no false violations.
- **The controls held.** No out-of-scope request reached the application, and every conflict was sent to human review without a confirmed finding.
- **Confirming is harder than finding.** Whenever the application started, the Executor triggered the defect. Most misses are `inconclusive` findings that cite the symptom, because the Verifier cannot yet rule out that something else changed the application state.

### Limitations

These results come from one test application, one model and three runs, and only the project team reviewed the defect labels. Three adjustments made after reviewing the initial results are documented in the repository.

## Reviewing the implementation

A suggested reading order for reviewers:

1. **Contracts.** [domain/requirements.py](src/testing_agent/domain/requirements.py), [domain/experiments.py](src/testing_agent/domain/experiments.py) and [domain/findings.py](src/testing_agent/domain/findings.py) define what a requirement, a plan and a finding are, and what each must cite.
2. **The graph.** [orchestration/graph.py](src/testing_agent/orchestration/graph.py), [routing.py](src/testing_agent/orchestration/routing.py) and [policies.py](src/testing_agent/orchestration/policies.py) show how the three agents are connected and how budgets stop them.
3. **The agents and their prompts.** [agents/](src/testing_agent/agents/) and [prompts/](src/testing_agent/prompts/).
4. **The guardrails.** [tools/application.py](src/testing_agent/tools/application.py) (scope checks on every request), [integrations/target/http_client.py](src/testing_agent/integrations/target/http_client.py) and [tools/sandbox.py](src/testing_agent/tools/sandbox.py) (private transport and containment).
5. **One investigation end to end.** [runner.py](src/testing_agent/runner.py), shared by the CLI, the web application and the evaluation harness.
6. **Evidence of behaviour.** [tests/integration/test_intervention.py](tests/integration/test_intervention.py) (pauses and human review), [tests/integration/test_mvp_evaluation.py](tests/integration/test_mvp_evaluation.py) (guardrail cases) and [tests/integration/test_evaluation_controlled.py](tests/integration/test_evaluation_controlled.py) (the 52-case harness with scripted models).

## Scope and limitations

- **Isolated targets only.** The MVP investigates REST APIs with a structured, versioned requirement catalog, against an isolated instance. Production systems and real data are out of scope; a run against an external URL pauses for operational review.
- **No global guarantees.** A correct local sequence does not prove a global invariant, and `no_violation_observed` does not prove the application is free of defects.
- **Isolation is not proven.** The same `instance_id` does not prove isolation, and the run context reports `external_modifications_excluded: false`. This is the main reason the Verifier leaves some real defects `inconclusive`.
- **One experiment at a time.** The design was inspired by LATS (environment feedback decides whether to continue, re-plan or stop), but no tree search is implemented.
- **Not yet implemented:** resuming an investigation from a SQLite checkpoint, recommendations in the report, consumption of the OpenAPI description, and long-term memory across investigations.

## Technology

Python 3.12 · LangGraph · LangChain chat models · Model Context Protocol SDK (FastMCP, stdio) · Pydantic v2 · HTTPX · Starlette (test application) · SQLite checkpoints · Streamlit (web application) · Conda.
