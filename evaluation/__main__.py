import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from evaluation.cases import GROUPS, MODES, ROOT, load_cases
from evaluation.runner import DEFAULT_SETTINGS, replay_case, run_case, run_sync
from evaluation.scoring import score
from testing_agent.settings import load_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evaluation",
        description=(
            "Command line: list the cases, run a mode, or score a saved report again.\n"
            "\n"
            "    python -m evaluation list\n"
            "    python -m evaluation run --mode controlled\n"
            "    python -m evaluation run --mode replay --reps 3 --yes\n"
            "    python -m evaluation run --mode real --reps 3 --yes\n"
            "\n"
            "`replay` and `real` call the model configured in `config/application.json` and cost tokens;\n"
            "they refuse to start without `--yes`.\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="Show the labelled cases.")
    run = commands.add_parser("run", help="Run the cases in one mode and write report.json.")
    run.add_argument("--mode", choices=MODES, required=True)
    run.add_argument("--reps", type=int, default=1, help="Repetitions per case (replay and real).")
    run.add_argument("--case", action="append", help="Only this case id; repeat for several.")
    run.add_argument("--group", action="append", choices=GROUPS, help="Only this group; repeat for several.")
    run.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)
    run.add_argument("--output", type=Path, help="Directory for the report. Default: var/evaluations/<uuid>.")
    run.add_argument("--yes", action="store_true", help="Accept the model calls that replay and real make.")
    again = commands.add_parser("score", help="Score a saved report.json again.")
    again.add_argument("report", type=Path)

    return parser


def _select(cases: list[dict[str, Any]], args: argparse.Namespace, mode: str) -> list[dict[str, Any]]:
    chosen = [case for case in cases if mode in case["modes"]
              and (not args.group or case["group"] in args.group)
              and (not args.case or case["id"] in args.case)]
    unknown = set(args.case or []) - {case["id"] for case in cases}

    if unknown:
        raise SystemExit(f"Unknown case id(s): {sorted(unknown)}")

    if not chosen:
        raise SystemExit("No case matches the selection for this mode.")

    return chosen


def _models(settings_path: Path) -> dict[str, str]:
    """Provider and model per agent. Never the credentials location."""
    settings = load_settings((ROOT / settings_path).resolve())
    result = {}

    for agent, profile in settings["agents"].items():
        entry = settings["llm"][profile]
        result[agent] = f"{entry['provider']}:{entry['model']}"

    return result


def _show(metrics: dict[str, Any]) -> None:
    def fmt(item: dict[str, Any]) -> str:
        if item["value"] is None:
            return f"n/a ({item['n']}/{item['of']})"

        low, high = item["ci95"]

        return f"{item['value']:.2f} ({item['n']}/{item['of']}, 95% CI {low:.2f}-{high:.2f})"

    detection = metrics["detection"]
    print(f"\nRuns scored: {metrics['runs']}  excluded: {metrics['excluded_runs'] or 0}")
    print(f"Detection      TP={detection['tp']}  FP={detection['fp']}  FN={detection['fn']}")
    print(f"  precision    {fmt(detection['precision'])}")
    print(f"  recall       {fmt(detection['recall'])}")
    print(f"  exposure     {fmt(detection['exposure'])}   (defect reached by the sequence that ran)")
    print(f"  recall|exposed {fmt(detection['recall_given_exposure'])}")

    if "detected_or_flagged" in detection:
        print(f"  flagged too  {fmt(detection['detected_or_flagged'])}   (violation, or inconclusive citing the symptom)")

    print(f"  false alarms {fmt(detection['false_alarm_runs_without_defect'])}   (runs without a defect)")

    if "clean_runs_left_inconclusive" in detection:
        print(f"  over-caution {fmt(detection['clean_runs_left_inconclusive'])}   "
              "(clean runs whose requirement stayed inconclusive; INVENTORY cases excluded)")

    print(f"  misses       {detection['misses'] or 'none'}")
    print("  by requirement (defect runs / detected / missed / false alarms):")

    for requirement, item in metrics["by_requirement"].items():
        print(f"    {requirement:<22} {item.get('defects', 0):>3} {item.get('tp', 0):>3} "
              f"{item.get('fn', 0):>3} {item.get('fp', 0):>3}")

    causes = {"inconclusive", "not_exposed", "no_violation_reported", "unsupported_violation",
              "operational_pause", "no_verdict"}
    undetected = {case: runs for case, runs in metrics["per_case"].items() if causes & set(runs)}

    if undetected:
        print("  defects not detected in every repetition:")

        for case, runs in undetected.items():
            print(f"    {case:<42} {', '.join(runs)}")

    controls = metrics["execution_controls"]
    print(f"Execution      out-of-scope requests that reached the app: {controls['out_of_scope_reaching_application']}"
          f" of {controls['requests_reaching_application']};  authorized steps blocked by the controller:"
          f" {controls['authorized_steps_blocked_by_controller']};  blocked: {controls['blocked_decisions'] or 'none'}")
    human = metrics["human_intervention"]
    print(f"Intervention   ambiguous cases paused and unconfirmed {fmt(human['ambiguous_cases_paused_unconfirmed'])};"
          f" review requested where none was needed {fmt(human['unnecessary_pauses_elsewhere'])};"
          f" operational pauses {human['operational_pauses_elsewhere'] or 'none'}")
    print("Agreement with the label (conclusion on the case's own requirement, and status):")

    for group, item in metrics["agreement_with_label"].items():
        print(f"  {group:<13} {fmt(item)}")


async def _execute(args: argparse.Namespace, selected: list[dict[str, Any]], reps: int,
                   area: Path) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []

    if args.mode == "replay":
        baseline = []

        for case in selected:
            observation, directory = await run_case(case, 1, "controlled", area / "executions")

            if observation["error"] or directory is None:
                raise SystemExit(f"The scripted execution of {case['id']} failed: {observation['error']}")

            baseline.append((case, observation, directory))

        for case, observation, directory in baseline:
            for rep in range(1, reps + 1):
                observations.append(await replay_case(case, rep, observation, directory, args.settings))
                print(f"  {case['id']} #{rep}: {observations[-1]['outcome'] or observations[-1]['error']}",
                      file=sys.stderr)

    else:
        for case in selected:
            for rep in range(1, reps + 1):
                observation, _ = await run_case(case, rep, args.mode, area / "executions", args.settings)
                observations.append(observation)
                print(f"  {case['id']} #{rep}: {observation['outcome'] or observation['error']}", file=sys.stderr)

    return observations


def _run(args: argparse.Namespace) -> int:
    cases = load_cases()
    by_id = {case["id"]: case for case in cases}
    selected = _select(cases, args, args.mode)
    reps = 1 if args.mode == "controlled" else args.reps

    if args.mode != "controlled":
        models = _models(args.settings)
        calls = len(selected) * reps
        what = "Verifier calls" if args.mode == "replay" else "full investigations (Planner, Executor and Verifier, several calls each)"
        print(f"{args.mode}: {calls} {what} with {models}.", file=sys.stderr)

        if not args.yes:
            print("Add --yes to run them.", file=sys.stderr)

            return 2

    area = (ROOT / (args.output or Path("var/evaluations") / str(uuid4()))).resolve()
    area.mkdir(parents=True, exist_ok=True)
    # One event loop for everything: provider clients are cached and bound to the loop that made them.
    observations = run_sync(_execute(args, selected, reps, area))
    metrics = score(by_id, observations)
    report = {
        "mode": args.mode, "created": datetime.now(UTC).isoformat(), "reps": reps,
        "models": _models(args.settings) if args.mode != "controlled" else "scripted",
        "cases": {case["id"]: {key: case[key] for key in ("group", "requirement", "mutant", "defect_requirements",
                                                              "expected", "operations", "modes") if key in case}
                  for case in selected},
        "observations": observations, "metrics": metrics,
    }
    (area / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Report: {area / 'report.json'}")
    _show(metrics)

    if args.mode == "controlled":
        print("\nControlled mode checks the harness and the controller. Its detection figures say nothing "
              "about a language model.")

    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.command == "list":
        for case in load_cases():
            print(f"{case['group']:<12} {case['id']:<40} {case['requirement']:<22} "
                  f"{case['mutant'] or '-':<34} {','.join(case['modes'])}")

        return 0

    if args.command == "score":
        report = json.loads(args.report.read_text(encoding="utf-8"))
        cases = {case["id"]: case for case in load_cases()}
        _show(score(cases, report["observations"]))

        return 0

    return _run(args)


if __name__ == "__main__":
    raise SystemExit(main())
