# Demo case DEMO-01: the retry that manufactures inventory

This document summarizes the case used to demonstrate the system and explains how to run it. It complements the [README](README.md).

## 1. The case in brief

**The story.** A client cancels a reservation, its request times out, and it retries the cancellation. The defective application answers both times exactly alike (HTTP 200, reservation `CANCELLED`, the same body), but on the retry it returns the units to stock a second time. With 10 real units, the stock rises to 13 and then to 16, and the system starts selling units that do not exist. **No single response, seen on its own, is wrong.** Only the sequence reveals the defect.

**What the demo shows.** Two systems that look identical to the agent are registered in the web application, one clean and one with the defect, and the same investigation is run on each:

| System | What it is | Expected finding for `REQ-RESERVATION-004` |
|---|---|---|
| **Inventory & Reservations** (the original) | The test application, no defect | `no_violation_observed`: the stock stays at 10 after every retry |
| **Inventory & Reservations (variant B)** | The same application with a seeded defect | `violation`: the stock rises by 3 units after every retry |

The agent does not know which one is defective. It sees no code and no seed files, only HTTP responses. The demo shows that it **finds the defect where it exists, does not invent it where it does not, and states what it cannot conclude**.

## 2. The rule and the defect

**The rule** (`REQ-RESERVATION-004` in [requirements.json](specifications/target_app/v1/requirements.json)):

> *Description:* Cancel a reservation and restore its units exactly once.
>
> *Exception `ALREADY_CANCELLED`:* "The reservation exists and is already CANCELLED." → "Respond with HTTP 200 with the same id, product_id, quantity, and status equal to CANCELLED. Do not increase stock or modify any reservation. Repeating the cancellation has the same effect as executing it once."

**The defect** (`cancel_restores_twice`, in [inventory.py](sandbox/target_app/src/inventory.py)): when the reservation is already `CANCELLED`, the cancellation adds its quantity to stock again. The HTTP response does not change. The fault is only switched on by a seed file that asks for it, and the possible fault names are a closed list (`KNOWN_FAULTS`).

**The two systems differ in one line.** Both are defined by an importable JSON file with the same purpose, the same four endpoints and the same six requirements:

| | Original | Variant B |
|---|---|---|
| System file | [inventory_system.json](config/examples/inventory_system.json) | [inventory_system_variant_b.json](config/examples/inventory_system_variant_b.json) |
| `target` | `{"kind": "managed_sandbox"}` | `{"kind": "managed_sandbox", "seed": "products_cancel_retry.json"}` |
| Seed | [products.json](sandbox/target_app/seed/products.json) | [products_cancel_retry.json](sandbox/target_app/seed/products_cancel_retry.json): same products plus `"faults": ["cancel_restores_twice"]` |

The seed names the initial data of the sandbox (`product-001` with 10 units, `product-002` with 2, `product-003` with 0). The name and purpose of variant B do not hint at the defect, because the Planner receives them. The seed never reaches the agents: neither the execution context nor the initial state mentions it.

`REQ-INVENTORY-001` (stock equals the initial quantity minus the units of `ACTIVE` reservations) is also involved, but its preconditions (a reset to a known state and no external changes) cannot be established with the authorized operations. It is therefore expected to stay `inconclusive`.

## 3. See the defect without a model

```bash
conda activate testing-agent
python -m demo
```

[demo/__main__.py](demo/__main__.py) plays the scenario against both applications with real HTTP requests over an in-process transport. No model and no API key are involved. Output (reservation IDs vary):

```text
=== CLEAN application ===
 #  step                   request                                          HTTP  observed
 1  read the stock         GET /products/product-001                         200  stock = 10
 2  reserve 3 units        POST /reservations                                201  ACTIVE, quantity 3
 3  read the stock         GET /products/product-001                         200  stock = 7
 4  cancel                 POST /reservations/reservation-..21559f/cancel    200  CANCELLED, quantity 3
 5  read the stock         GET /products/product-001                         200  stock = 10
 6  cancel again (retry)   POST /reservations/reservation-..21559f/cancel    200  CANCELLED, quantity 3
 7  read the stock         GET /products/product-001                         200  stock = 10
 8  reserve 13 units       POST /reservations                                409  error INSUFFICIENT_STOCK

=== DEFECTIVE application ===
 #  step                   request                                          HTTP  observed
 1  read the stock         GET /products/product-001                         200  stock = 10
 2  reserve 3 units        POST /reservations                                201  ACTIVE, quantity 3
 3  read the stock         GET /products/product-001                         200  stock = 7
 4  cancel                 POST /reservations/reservation-..cd42af/cancel    200  CANCELLED, quantity 3
 5  read the stock         GET /products/product-001                         200  stock = 10
 6  cancel again (retry)   POST /reservations/reservation-..cd42af/cancel    200  CANCELLED, quantity 3
 7  read the stock         GET /products/product-001                         200  stock = 13
 8  reserve 13 units       POST /reservations                                201  ACTIVE, quantity 13

=== What each check sees ===
                                              clean         defective
Per-response contract (status + body shape)   pass          pass
Retry answers with the same body as the first pass          pass
Inventory invariant (REQ-INVENTORY-001)       pass          FAIL
  defective, invariant: #7: stock reads 13, initial stock minus ACTIVE units is 10
  defective, invariant: #8: ACTIVE reservations hold 13 units but only 10 exist
```

A per-response check passes on both applications. Step 8 shows the business consequence: 13 units sold out of 10. This script is the reference for what the agent has to discover on its own, from the requirement text alone.

## 4. Run the demo in the web application

Each investigation calls the configured model (about US$0.10 per run with `gpt-6.1-sol`) and takes about two minutes. Setup and credentials are described in the [README](README.md#setup).

### 4.1 Start the application

```bash
conda activate testing-agent
python -m testing_agent_app --settings config/demo.json
```

Open `http://127.0.0.1:8765`. **`--settings config/demo.json` is required:** it raises the limits from 10 calls and 120 s to 12 calls and 300 s. The time budget covers Planner, Executor and Verifier together, and with the default limits a 10-call sequence can run out of time before the Verifier judges it. Before running, check that the limits line on the new-investigation screen reads 12 calls and 300 s.

### 4.2 Register the two systems

1. **Systems → Register a new system → Import from a file.**
2. In the drop-down list, choose `config/examples/inventory_system.json` and load it.
3. Repeat with `config/examples/inventory_system_variant_b.json`.

Two systems should appear, *Inventory & Reservations* and *Inventory & Reservations (variant B)*, each with 4 endpoints and 6 requirements. Use the drop-down list, not *Or upload your own file*: an uploaded file must contain its requirements inline, and these example files reference the catalog by path.

### 4.3 Investigate variant B

Open the system, go to the *Endpoints* tab and choose **Test this endpoint** on `POST /reservations/{reservation_id}/cancel`. Fill in the form:

| Field | Value |
|---|---|
| 1 · Endpoint | `POST /reservations/{reservation_id}/cancel` |
| 2 · Requirement | `REQ-RESERVATION-004` (preselected) |
| 3 · Extra requirement or detail | the text below |
| 4 · Setup operations | `POST /reservations` and `GET /products/{product_id}` |

Extra detail (use the same text for both systems):

> A client may repeat the cancellation of a reservation, for example when it retries after a network timeout. Design the sequence with controls, so that any change in stock can be attributed to the operation under test: read the stock twice with no operation in between before the first retry, reserve a quantity larger than one so that the expected change is distinctive, and repeat the cancellation more than once.

Open *Instruction sent to the Planner* to confirm it contains the extra detail, tick the cost confirmation and choose **Run investigation**. The investigation screen shows progress: plan ready, execution recorded, evaluation finished.

**Why the extra detail and the setup operations matter.** With only the cancel operation authorized, the agent cannot create a reservation or read the stock. It cancels a non-existent ID, receives the correct 404 (also on variant B) and closes `inconclusive`. That result is correct, but the defect is never triggered. The extra detail supplies the business scenario (a retry) and a methodological criterion (controls); it does not reveal which system is defective.

### 4.4 Investigate the original and compare

When the first investigation finishes, repeat the same form from the **Inventory & Reservations** system. The application runs one investigation at a time. Then open both investigations (each system's *Investigations* tab or the global history) and compare the stock readings and the finding for `REQ-RESERVATION-004`.

### 4.5 Alternative: command line

The defective system can also be investigated without the interface. This is not how the reference runs below were produced, and the objective is free text rather than the one the interface builds:

```powershell
python -m testing_agent.cli run --settings config/demo.json `
  --sandbox --seed-file sandbox/target_app/seed/products_cancel_retry.json `
  --requirements specifications/target_app/v1/requirements.json --version v1 `
  --operation POST "/reservations/{reservation_id}/cancel" `
  --operation POST "/reservations" `
  --operation GET "/products/{product_id}" `
  --objective "Investigate REQ-RESERVATION-004 on POST /reservations/{reservation_id}/cancel. A client may repeat the cancellation of a reservation, for example when it retries after a network timeout. Design the sequence with controls, so that any change in stock can be attributed to the operation under test: read the stock twice with no operation in between before the first retry, reserve a quantity larger than one so that the expected change is distinctive, and repeat the cancellation more than once."
```

Omit `--seed-file` to investigate the original. On bash, replace the trailing backticks with `\`.

## 5. Reference runs and how to read them

Two real investigations were run through the interface with `gpt-6.1-sol` and the configuration above. Their artifacts were stored locally under `var/runs/<run_id>/` and are not part of the repository.

| System | Run | Finding for `REQ-RESERVATION-004` | Stock readings, in order | Duration |
|---|---|---|---|---|
| Variant B | `501d7034-…` | **`violation`** | 10, 7, 10, 10, **13**, **16** | 118 s |
| Original | `cfdbe2ca-…` | **`no_violation_observed`** | 10, 7, 10, 10, 10, 10 | 145 s |

### 5.1 Variant B: `violation`

**The plan** (`experiment.json`) had 10 steps: baseline read, reserve 3 units, read the held stock, first cancellation, read, a *quiet control* (a second read with no operation in between), first retry, read, second retry, read. Its hypothesis: cancelling an already-cancelled reservation may restore its quantity again. The quiet control is a gate: if the stock changed between two consecutive reads, nothing could be attributed to the retry.

**The 10 Executor calls** (`evidence/trace.json`):

| # | Operation | Response | Stock |
|--:|---|---|--:|
| 1 | `GET /products/product-001` | 200 | 10 |
| 2 | `POST /reservations` (3 units) | 201 `ACTIVE` | |
| 3 | `GET /products/product-001` | 200 | 7 |
| 4 | `POST …/cancel` | 200 `CANCELLED` | |
| 5 | `GET /products/product-001` | 200 | 10 |
| 6 | `GET /products/product-001` | 200 | 10 (control) |
| 7 | `POST …/cancel` (retry 1) | 200 `CANCELLED` | |
| 8 | `GET /products/product-001` | 200 | **13** |
| 9 | `POST …/cancel` (retry 2) | 200 `CANCELLED` | |
| 10 | `GET /products/product-001` | 200 | **16** |

**The Verifier's findings:**

| Requirement | Outcome | Reading |
|---|---|---|
| `REQ-RESERVATION-004` | **`violation`**, citing 9 calls | Each retry returned 200 with the same `CANCELLED` body, but the stock rose by exactly 3 after each one, following two stable reads. `ALREADY_CANCELLED` requires that stock does not increase. |
| `REQ-INVENTORY-001` | `inconclusive`, citing 3 calls | It saw 13 and 16 (more than the initial 10), but this global invariant depends on a reset and on the absence of external changes, which cannot be established. |

The Verifier also stated its limitations: external modifications are not excluded (the stable controls and repeated increments support attribution but do not prove exclusive access), the reservation's persisted state is only known from the cancel responses, and the request body, the 404 path and concurrency were not tested.

**Calibration is the point to highlight:** the same fact (stock above 10) yields `violation` for the local requirement and `inconclusive` for the global invariant, because only the latter depends on preconditions that cannot be established.

### 5.2 Original: `no_violation_observed`, global `inconclusive`

**Round 1** followed a 10-step plan of the same structure. The stock read 10, 7, 10, 10, 10, 10, and the finding for `REQ-RESERVATION-004` was `no_violation_observed`, citing 10 calls: the observations agree with restoring the units exactly once and with the `ALREADY_CANCELLED` contract. The agent did not invent a defect.

**Round 2.** The Verifier was not done: the same requirement says the cancel request body is ignored, and that part had not been tested. It sent the case back to the Planner, which formed a different hypothesis (a cancel request with conflicting `product_id`, `quantity` and `status` in the body). The Executor made only 2 calls before the investigation hit the 12-call limit (`tool_limit`), and the Verifier closed with a second, `inconclusive` finding for that untested part.

How to read it:

- The `no_violation_observed` finding (stable stock) and the `inconclusive` finding (conflicting body, not reached) cover **different scopes** of the same requirement. They do not contradict each other.
- The global `inconclusive` does not mean the original has a problem. It means the agent wanted to check more and ran out of call budget. By design, it does not certify what it did not test.

### 5.3 Reading any report

- **Global outcome versus findings:** the global outcome is `violation` if any finding is a violation, `no_violation_observed` only if all findings are, and `inconclusive` otherwise.
- **Plan:** hypothesis, steps, dependencies, expected observations and stop conditions. It is intent, not execution.
- **Operations:** what actually reached the application, in order. This is the evidence.
- **Citations:** each finding points to calls with their responses (`evidence_id`, `tool_call_id`). A `violation` or `no_violation_observed` always cites at least one; an `inconclusive` finding may cite none.
- **Limitations:** what the agent declares it could not check.

## 6. What can and cannot be claimed

**Supported:**

- With the defect, every response is individually correct and only the stock reveals the problem (model-free script, also checked by [tests/unit/test_demo.py](tests/unit/test_demo.py) and [tests/unit/test_sandbox_faults.py](tests/unit/test_sandbox_faults.py)).
- In the reference run, the agent designed a sequence with controls, executed it within the authorized scope and reported `violation` citing specific calls; on the original, with the same investigation, it reported no violation.
- The agent states its limits and does not certify the application.

**Not supported:**

- That it finds *any* idempotency defect, or that an application is defect-free when no violation is observed.
- That it found the defect unaided. The extra detail contains the scenario (a retry) and a methodological hint (controls). In three earlier trial runs without that hint, the agent executed the sequence, observed the symptom and left the finding `inconclusive`. A defensible statement is: *given a business scenario and a methodological criterion, the agent designs, executes and justifies the verification.*
- A detection rate, or that the result reproduces run after run. These are two reference runs with one model on one synthetic application seeded by the team. The measured detection rates are in the [README evaluation section](README.md#evaluation-results).

## 7. Troubleshooting

| Situation | What to do |
|---|---|
| Variant B ends `inconclusive` | Not a failure: the Verifier cites the symptom but does not attribute the increase, because `external_modifications_excluded` is `false`. Rerun if needed (about 2 minutes). |
| The original ends globally `inconclusive` | Expected when the Verifier opens a second hypothesis (5.2). Focus on the repeated-cancellation finding. |
| The limits line shows 10 calls and 120 s | The application was started without `--settings config/demo.json`. |
| The run never creates a reservation | The setup operations or the extra detail were not included (4.3). |
| The case pauses with `recovery_required` or `startup_unverified` | A guardrail or a start-up failure, not a verdict. Start a new investigation. |
| Deleting a system fails with `[WinError 5]` | A known issue when the folder is synced by OneDrive. Reuse existing systems instead of deleting them. |
