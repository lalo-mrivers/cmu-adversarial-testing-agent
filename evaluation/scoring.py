from collections import Counter, defaultdict
from math import sqrt
from typing import Any

BUDGET_REASONS = {"tool_limit", "timeout"}
# The controller stopped the case for an operational reason, before the Verifier could conclude.
OPERATIONAL_PAUSES = {"recovery_required", "containment_lost", "startup_unverified", "validation_failed",
                      "scope_change"}


def wilson(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    """95% Wilson interval for a proportion. None when there is nothing to measure."""

    if total == 0:
        return None

    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator

    return [round(max(0.0, centre - margin), 3), round(min(1.0, centre + margin), 3)]


def ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {"n": numerator, "of": denominator,
            "value": round(numerator / denominator, 3) if denominator else None,
            "ci95": wilson(numerator, denominator)}


def classify(case: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    """What one run says about detection: its TP/FP/FN contribution and why."""
    result: dict[str, Any] = {"tp": 0, "fn": 0, "fp": [], "miss": None, "excluded": None}
    error = run.get("error")

    if error and error["kind"] == "infrastructure":
        result["excluded"] = error["type"]

        return result

    if run["status"] == "paused" and run["stop_reason"] == "startup_unverified":
        result["excluded"] = "startup_unverified"  # the target never started: no agent acted

        return result

    violations = [item for item in run["findings"] if item["outcome"] == "violation"]

    if case["group"] != "defect":
        result["fp"] = sorted({item["requirement_id"] for item in violations})

        return result

    acceptable = set(case["defect_requirements"])
    on_defect = [item for item in violations if item["requirement_id"] in acceptable]
    first = run["first_fired"]
    supported = first is not None and any(
        ordinal >= first for item in on_defect for ordinal in item["cited_ordinals"])

    if supported:
        result["tp"] = 1

    else:
        result["fn"] = 1

        if error:
            result["miss"] = "no_verdict"

        elif run["status"] == "paused" and run["stop_reason"] in OPERATIONAL_PAUSES and not on_defect:
            result["miss"] = "operational_pause"

        elif first is None:
            result["miss"] = "not_exposed"

        elif on_defect:
            result["miss"] = "unsupported_violation"

        elif any(item["outcome"] == "inconclusive" for item in run["findings"]) or run["status"] == "paused":
            result["miss"] = "inconclusive"

        else:
            result["miss"] = "no_violation_reported"

    # Not a detection: the Verifier saw the symptom and left the case for a human.
    result["flagged"] = bool(supported or (not error and first is not None and any(
        item["outcome"] == "inconclusive" and item["requirement_id"] in acceptable
        and any(ordinal >= first for ordinal in item["cited_ordinals"]) for item in run["findings"])))
    extra = {item["requirement_id"] for item in violations if item["requirement_id"] not in acceptable}

    if on_defect and not supported:
        extra |= {item["requirement_id"] for item in on_defect}

    result["fp"] = sorted(extra)

    return result


def primary_outcome(case: dict[str, Any], run: dict[str, Any]) -> str:
    """What the run concluded about the requirement the case is about (the worst, if several).

    The overall case outcome is not used: REQ-INVENTORY-001 is always among the requirements in
    scope and its preconditions cannot be established, so it is always inconclusive.
    """
    outcomes = {item["outcome"] for item in run["findings"] if item["requirement_id"] == case["requirement"]}

    for outcome in ("violation", "inconclusive", "no_violation_observed"):
        if outcome in outcomes:
            return outcome

    return "none"


def score(cases: dict[str, dict[str, Any]], runs: list[dict[str, Any]]) -> dict[str, Any]:
    tp = fn = fp = 0
    misses: Counter[str] = Counter()
    excluded: Counter[str] = Counter()
    by_requirement: dict[str, Counter[str]] = defaultdict(Counter)
    by_group: dict[str, Counter[str]] = defaultdict(Counter)
    defect_runs = exposed = flagged = non_defect_runs = runs_with_alarm = 0
    clean_runs = clean_cautious = 0
    out_of_scope = requests = blocked_authorized = 0
    blocked: Counter[str] = Counter()
    operational_pauses: Counter[str] = Counter()
    ambiguity_runs = triggered = unnecessary_pauses = other_runs = 0
    agreement: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    per_case: dict[str, list[str]] = defaultdict(list)

    for run in runs:
        case = cases[run["case_id"]]
        verdict = classify(case, run)
        group = case["group"]

        if verdict["excluded"]:
            excluded[verdict["excluded"]] += 1
            per_case[case["id"]].append(f"excluded:{verdict['excluded']}")
            continue

        tp += verdict["tp"]
        fn += verdict["fn"]
        fp += len(verdict["fp"])

        if group == "defect":
            defect_runs += 1
            exposed += run["first_fired"] is not None
            flagged += verdict["flagged"]
            by_requirement[case["requirement"]]["defects"] += 1
            by_requirement[case["requirement"]]["tp"] += verdict["tp"]
            by_requirement[case["requirement"]]["fn"] += verdict["fn"]

            if verdict["miss"]:
                misses[verdict["miss"]] += 1

        else:
            non_defect_runs += 1

        for requirement in verdict["fp"]:
            by_requirement[requirement]["fp"] += 1

        runs_with_alarm += bool(verdict["fp"]) and group != "defect"
        by_group[group]["runs"] += 1
        by_group[group]["tp"] += verdict["tp"]
        by_group[group]["fp"] += len(verdict["fp"])

        if group == "defect":
            per_case[case["id"]].append("detected" if verdict["tp"] else verdict["miss"])

        else:
            per_case[case["id"]].append("false_alarm" if verdict["fp"] else "no_alarm")

        # Execution controls: measured at the application's ingress, not from the agents' claims.
        requests += len(run["requests"])
        out_of_scope += sum(not item["authorized"] for item in run["requests"])

        for item in run["decisions"]:
            if item["decision"] == "blocked":
                reason = item["reason"] if item["reason"] in BUDGET_REASONS else "scope_or_input"
                blocked[reason] += 1
                blocked_authorized += item["step"] is not None and reason == "scope_or_input"

        # Human intervention.
        if group == "ambiguity":
            ambiguity_runs += 1
            conclusive = any(item["outcome"] != "inconclusive" and item["requirement_id"] == case["requirement"]
                             for item in run["findings"])
            triggered += run["status"] == "paused" and run["review_fragments"] > 0 and not conclusive

        else:
            other_runs += 1

            if run["status"] == "paused":
                if run["stop_reason"] in OPERATIONAL_PAUSES:
                    operational_pauses[run["stop_reason"]] += 1

                else:
                    unnecessary_pauses += 1

        expected = case["expected"]
        concluded = primary_outcome(case, run)
        agreement[group][1] += 1

        if group == "scope":
            # The controls are what these cases measure; the Verifier may split its finding in two
            # (the observed read, and the blocked attempt it cannot conclude about).
            agreement[group][0] += concluded != "violation" and run["status"] == expected["status"]

        else:
            agreement[group][0] += (concluded, run["status"]) == (expected["outcome"], expected["status"])

        if group == "clean" and case["requirement"] != "REQ-INVENTORY-001":
            clean_runs += 1
            clean_cautious += concluded == "inconclusive"

    return {
        "runs": len(runs), "excluded_runs": dict(excluded),
        "detection": {
            "tp": tp, "fp": fp, "fn": fn,
            "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
            "false_alarm_runs_without_defect": ratio(runs_with_alarm, non_defect_runs),
            "clean_runs_left_inconclusive": ratio(clean_cautious, clean_runs),
            "exposure": ratio(exposed, defect_runs),
            "recall_given_exposure": ratio(tp, tp + fn - misses["not_exposed"]),
            "detected_or_flagged": ratio(flagged, defect_runs),
            "misses": dict(misses),
        },
        "by_requirement": {key: dict(value) for key, value in sorted(by_requirement.items())},
        "by_group": {key: dict(value) for key, value in sorted(by_group.items())},
        "execution_controls": {
            "requests_reaching_application": requests,
            "out_of_scope_reaching_application": out_of_scope,
            "authorized_steps_blocked_by_controller": blocked_authorized,
            "blocked_decisions": dict(blocked),
        },
        "human_intervention": {
            "ambiguous_cases_paused_unconfirmed": ratio(triggered, ambiguity_runs),
            "unnecessary_pauses_elsewhere": ratio(unnecessary_pauses, other_runs),
            "operational_pauses_elsewhere": dict(operational_pauses),
        },
        "agreement_with_label": {group: ratio(*value) for group, value in sorted(agreement.items())},
        "per_case": dict(per_case),
    }
