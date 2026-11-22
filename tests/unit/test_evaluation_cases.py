import asyncio
import json
from collections import Counter

import pytest

from evaluation.cases import (CATALOG, GROUPS, judge, load_cases, operation_pairs, run_inprocess,
                              variant_catalog)
from evaluation.mutants import MUTANTS
from evaluation.scoring import classify, score, wilson

CASES = load_cases()
BY_ID = {case["id"]: case for case in CASES}
STEPPED = [case for case in CASES if case["group"] in ("defect", "clean")]


def clauses(rule):
    texts = [rule["description"], rule["expected_behavior"], *rule["preconditions"]]

    for exception in rule["exceptions"]:
        texts += [exception["condition"], exception["expected_behavior"]]

    return texts


def test_the_set_is_large_and_balanced():
    counts = Counter(case["group"] for case in CASES)
    assert set(counts) == set(GROUPS)
    assert len(CASES) == 52
    assert counts["defect"] == counts["clean"] == 21
    assert len({case["id"] for case in CASES}) == len(CASES)


def test_every_mutant_is_seeded_once_and_every_requirement_has_defects():
    seeded = [case["mutant"] for case in CASES if case["group"] == "defect"]
    assert sorted(seeded) == sorted(MUTANTS)
    per_requirement = Counter(case["requirement"] for case in CASES if case["group"] == "defect")
    catalog = json.loads(CATALOG.read_text(encoding="utf-8-sig"))
    assert set(per_requirement) == {rule["id"] for rule in catalog["requirements"]}
    assert min(per_requirement.values()) >= 2


def test_pairs_share_their_sequence_and_differ_only_in_the_sandbox():
    for case in CASES:
        if case["group"] != "defect":
            continue

        twin = BY_ID[case["pair"]]
        assert BY_ID[twin["pair"]] is case
        assert twin["mutant"] is None

        for key in ("requirement", "objective", "operations", "steps", "clause"):
            assert twin[key] == case[key]


@pytest.mark.parametrize("case", [c for c in CASES if "clause" in c], ids=lambda c: c["id"])
def test_the_cited_clause_exists_in_the_requirement(case):
    rule = next(item for item in json.loads(CATALOG.read_text(encoding="utf-8-sig"))["requirements"]
                if item["id"] == case["requirement"])
    assert any(case["clause"] in text for text in clauses(rule)), case["clause"]


@pytest.mark.parametrize("case", [c for c in CASES if c["group"] == "defect"], ids=lambda c: c["id"])
def test_defect_requirements_exist_and_include_the_primary_one(case):
    known = {rule["id"] for rule in json.loads(CATALOG.read_text(encoding="utf-8-sig"))["requirements"]}
    assert case["requirement"] in case["defect_requirements"]
    assert set(case["defect_requirements"]) <= known


@pytest.mark.parametrize("case", [c for c in CASES if c["group"] == "ambiguity"], ids=lambda c: c["id"])
def test_ambiguity_fragments_quote_both_conflicting_clauses(case):
    catalog = variant_catalog(case)
    rules = {rule["id"]: rule for rule in catalog["requirements"]}
    assert len(case["review"]["fragments"]) == 2
    assert len({fragment["requirement_id"] for fragment in case["review"]["fragments"]}) == 2

    for fragment in case["review"]["fragments"]:
        assert any(fragment["quote"] in text for text in clauses(rules[fragment["requirement_id"]]))

    documented = {(op["method"], op["path"]) for rule in catalog["requirements"] for op in rule["operations"]}
    assert set(operation_pairs(case)) <= documented


@pytest.mark.parametrize("case", STEPPED, ids=lambda c: c["id"])
def test_on_the_sandbox_defects_are_observed_and_clean_twins_pass(case):
    responses, log = asyncio.run(run_inprocess(case))
    verdict = judge(case, responses)
    fired = [event for event in log if event["type"] == "fired"]

    if case["group"] == "defect":
        assert fired, "the seeded behaviour never ran"
        assert verdict["outcome"] == "violation", verdict

    else:
        assert not fired
        assert verdict["outcome"] == "no_violation_observed", verdict


@pytest.mark.parametrize("case", [c for c in CASES if c["group"] in ("scope", "ambiguity", "insufficient")],
                         ids=lambda c: c["id"])
def test_the_remaining_cases_describe_correct_application_behaviour(case):
    responses, _ = asyncio.run(run_inprocess(case))
    assert judge(case, responses)["outcome"] == "no_violation_observed"


# --------------------------------------------------------------------------- scoring


def observation(case_id, findings, *, fired=1, status="closed", outcome="no_violation_observed",
                requests=(), error=None, fragments=0, decisions=()):
    return {"case_id": case_id, "rep": 1, "mode": "real", "run_id": "r", "status": status, "outcome": outcome,
            "stop_reason": None, "review_fragments": fragments, "findings": findings,
            "decisions": list(decisions), "requests": list(requests), "first_fired": fired, "fired": [],
            "error": error}


def finding(requirement, outcome, ordinals=(1,)):
    return {"requirement_id": requirement, "outcome": outcome, "cited_ordinals": list(ordinals)}


DEFECT = BY_ID["cancel-repeat-defect"]
CLEAN = BY_ID["cancel-repeat-clean"]


def test_a_violation_citing_a_response_after_the_mutant_fired_is_detected():
    verdict = classify(DEFECT, observation(DEFECT["id"], [finding("REQ-RESERVATION-004", "violation", (5,))], fired=4))
    assert (verdict["tp"], verdict["fn"], verdict["fp"]) == (1, 0, [])


def test_a_violation_that_cites_only_earlier_responses_is_not_detection():
    verdict = classify(DEFECT, observation(DEFECT["id"], [finding("REQ-RESERVATION-004", "violation", (1, 2))], fired=4))
    assert (verdict["tp"], verdict["fn"], verdict["miss"]) == (0, 1, "unsupported_violation")
    assert verdict["fp"] == ["REQ-RESERVATION-004"]


@pytest.mark.parametrize("fired, findings, status, miss", [
    (None, [finding("REQ-RESERVATION-004", "no_violation_observed")], "closed", "not_exposed"),
    (3, [finding("REQ-RESERVATION-004", "no_violation_observed")], "closed", "no_violation_reported"),
    (3, [finding("REQ-RESERVATION-004", "inconclusive")], "closed", "inconclusive"),
])
def test_a_missed_defect_says_why(fired, findings, status, miss):
    assert classify(DEFECT, observation(DEFECT["id"], findings, fired=fired, status=status))["miss"] == miss


def test_an_inconclusive_finding_that_cites_the_symptom_is_flagged_but_not_detected():
    run = observation(DEFECT["id"], [finding("REQ-RESERVATION-004", "inconclusive", (5,))], fired=4)
    verdict = classify(DEFECT, run)
    assert (verdict["tp"], verdict["fn"], verdict["miss"], verdict["flagged"]) == (0, 1, "inconclusive", True)
    early = observation(DEFECT["id"], [finding("REQ-RESERVATION-004", "inconclusive", (1,))], fired=4)
    assert classify(DEFECT, early)["flagged"] is False
    metrics = score(BY_ID, [run])
    assert metrics["detection"]["recall"]["value"] == 0.0
    assert metrics["detection"]["detected_or_flagged"]["value"] == 1.0


def test_agreement_uses_the_requirement_under_test_not_the_overall_outcome():
    # REQ-INVENTORY-001 is always in scope and always inconclusive: it must not hide a clean conclusion.
    run = observation(CLEAN["id"], [finding("REQ-RESERVATION-004", "no_violation_observed"),
                                    finding("REQ-INVENTORY-001", "inconclusive")], fired=None, outcome="inconclusive")
    metrics = score(BY_ID, [run])
    assert metrics["agreement_with_label"]["clean"]["value"] == 1.0
    assert metrics["detection"]["clean_runs_left_inconclusive"]["n"] == 0
    cautious = observation(CLEAN["id"], [finding("REQ-RESERVATION-004", "inconclusive")], fired=None,
                           outcome="inconclusive")
    assert score(BY_ID, [cautious])["detection"]["clean_runs_left_inconclusive"]["n"] == 1


def test_a_run_whose_target_never_started_is_excluded_not_blamed_on_the_agent():
    run = observation(DEFECT["id"], [], fired=None, status="paused", outcome="inconclusive")
    run["stop_reason"] = "startup_unverified"
    assert classify(DEFECT, run)["excluded"] == "startup_unverified"
    metrics = score(BY_ID, [run])
    assert metrics["excluded_runs"] == {"startup_unverified": 1}
    assert metrics["detection"]["fn"] == 0


def test_a_run_stopped_by_the_controller_is_reported_apart_from_a_verifier_miss():
    run = observation(DEFECT["id"], [], fired=None, status="paused", outcome="inconclusive")
    run["stop_reason"] = "recovery_required"
    assert classify(DEFECT, run)["miss"] == "operational_pause"
    metrics = score(BY_ID, [{**run, "case_id": CLEAN["id"]}])
    assert metrics["human_intervention"]["operational_pauses_elsewhere"] == {"recovery_required": 1}
    assert metrics["human_intervention"]["unnecessary_pauses_elsewhere"]["n"] == 0


def test_a_violation_on_another_requirement_is_a_false_alarm_even_when_the_defect_is_found():
    verdict = classify(DEFECT, observation(DEFECT["id"], [
        finding("REQ-RESERVATION-004", "violation", (5,)), finding("REQ-PRODUCT-001", "violation", (3,))], fired=4))
    assert verdict["tp"] == 1 and verdict["fp"] == ["REQ-PRODUCT-001"]


def test_any_violation_on_a_clean_twin_is_a_false_alarm():
    verdict = classify(CLEAN, observation(CLEAN["id"], [finding("REQ-RESERVATION-004", "violation")], fired=None))
    assert verdict["fp"] == ["REQ-RESERVATION-004"] and verdict["tp"] == verdict["fn"] == 0


def test_an_agent_failure_is_a_miss_but_an_infrastructure_failure_is_excluded():
    agent = {"kind": "agent", "type": "ValueError", "message": ""}
    outage = {"kind": "infrastructure", "type": "APIConnectionError", "message": ""}
    assert classify(DEFECT, observation(DEFECT["id"], [], error=agent))["miss"] == "no_verdict"
    assert classify(DEFECT, observation(DEFECT["id"], [], error=outage))["excluded"] == "APIConnectionError"


def test_score_combines_detection_controls_and_intervention():
    ambiguous = BY_ID["ambiguity-product-identifier"]
    ok = {"authorized": True, "method": "GET", "path": "/x", "headers": [], "type": "request", "index": 1}
    leaked = {**ok, "authorized": False}
    runs = [
        observation(DEFECT["id"], [finding("REQ-RESERVATION-004", "violation", (5,))], fired=4, requests=[ok]),
        observation(CLEAN["id"], [finding("REQ-RESERVATION-004", "no_violation_observed")], fired=None,
                    requests=[ok, leaked],
                    decisions=[{"call_id": "call-0", "decision": "blocked", "reason": "x", "step": 0}]),
        observation(ambiguous["id"], [finding("REQ-PRODUCT-001", "inconclusive")], status="paused",
                    outcome="inconclusive", fragments=2),
    ]
    metrics = score(BY_ID, runs)
    assert (metrics["detection"]["tp"], metrics["detection"]["fp"], metrics["detection"]["fn"]) == (1, 0, 0)
    assert metrics["detection"]["precision"]["value"] == metrics["detection"]["recall"]["value"] == 1.0
    assert metrics["execution_controls"]["out_of_scope_reaching_application"] == 1
    assert metrics["execution_controls"]["authorized_steps_blocked_by_controller"] == 1
    assert metrics["human_intervention"]["ambiguous_cases_paused_unconfirmed"]["n"] == 1
    assert metrics["human_intervention"]["unnecessary_pauses_elsewhere"]["n"] == 0


def test_wilson_interval_is_wide_for_small_samples_and_absent_without_data():
    assert wilson(0, 0) is None
    low, high = wilson(20, 20)
    assert 0.8 < low < 1.0 and high == 1.0
    low, high = wilson(1, 1)
    assert low < 0.25
