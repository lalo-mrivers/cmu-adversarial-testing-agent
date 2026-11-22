import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from testing_agent.domain.requirements import (
    OperationReference,
    RequirementCatalog,
)
from testing_agent.integrations.mcp.server import (
    create_mcp_server,
    run_mcp_server,
)
from testing_agent.orchestration.policies import RunBudget
from testing_agent.persistence.redaction import redact
from testing_agent.settings import load_settings
from testing_agent.tools.application import create_application_request
from testing_agent.tools.requirements import create_requirements_lookup
from testing_agent.tools.sandbox import open_managed_target, restrict_target_process
from testing_agent.persistence.review import register_decision
from testing_agent.runner import RunConfig, RunControl, prepare_request, read_json, run_investigation



def _build_parser() -> argparse.ArgumentParser:
    """Defines the commands and their arguments."""
    parser = argparse.ArgumentParser(
        prog="testing-agent",
        description="Investigación de reglas de negocio mediante agentes.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    for command, description in (
        ("run", "Start an investigation."),
        ("serve", "Serve the MCP server over stdio."),
    ):
        subparser = commands.add_parser(command, help=description)

        subparser.add_argument(
            "--working-directory",
            type=Path,
            default=Path("."),
            help="Base directory used to resolve relative paths.",
        )
        subparser.add_argument(
            "--requirements",
            type=Path,
            required=True,
            help="JSON file for the requirement catalog.",
        )
        subparser.add_argument(
            "--version",
            required=True,
            help="Exact specification version.",
        )
        target = subparser.add_mutually_exclusive_group(required=True)
        target.add_argument("--base-url", help="External targets require operational review.")
        target.add_argument("--sandbox", action="store_true", help="Use the private inventory sandbox.")
        subparser.add_argument("--seed-file", type=Path)
        subparser.add_argument("--settings", type=Path, default=Path("config/application.json"))
        subparser.add_argument(
            "--operation",
            action="append",
            nargs=2,
            metavar=("METHOD", "PATH"),
            required=True,
            help="Authorized operation; repeat to allow several.",
        )
        subparser.add_argument(
            "--headers-file",
            type=Path,
            help="Absolute path to the external HTTP header JSON.",
        )

        if command == "run":
            subparser.add_argument("--objective", required=True)
            subparser.add_argument("--review-of", help="Paused run with an explicit supervisor decision.")
            subparser.add_argument(
                "--runs-path",
                type=Path,
                default=Path("var/runs"),
            )
            subparser.add_argument(
                "--checkpoint-path",
                type=Path,
                default=Path("var/checkpoints/checkpoints.sqlite"),
            )

    review = commands.add_parser("review", help="Register a supervisor decision on a paused run.")
    review.add_argument("--runs-path", type=Path, default=Path("var/runs"))
    review.add_argument("--run-id", required=True)
    review.add_argument("--decision-file", type=Path, required=True)
    review.add_argument("--requirements", type=Path)
    review.add_argument("--seed-file", type=Path, default=Path("sandbox/target_app/seed/products.json"))
    review.add_argument("--settings", type=Path, default=Path("config/application.json"))

    return parser


async def _serve(
    args: argparse.Namespace,
    catalog: RequirementCatalog,
    operations: list[OperationReference],
) -> None:
    """Keeps the HTTP session open while the MCP service is running."""
    lookup = create_requirements_lookup(
        args.requirements,
        catalog.specification_version,
    )

    if not args.sandbox or args.headers_file is not None:
        raise PermissionError("Only the private sandbox and its anonymous identity are authorized.")

    execution = load_settings(args.working_directory / args.settings)["execution"]
    budget = RunBudget(execution["max_tool_calls"], execution["timeout_seconds"])
    restrict_target_process(budget)

    async with open_managed_target(args.seed_file) as (client, context):
        limits = {key: execution[key] for key in ("max_iterations", "max_tool_calls", "timeout_seconds")}
        context.update(network_restricted=True, limits=limits, specification_version=catalog.specification_version,
                       allowed_operations=[op.model_dump() for op in operations])
        application_request = create_application_request(
            client,
            catalog,
            operations,
        )
        server = create_mcp_server(
            budget=budget, execution_context=context,
            requirements_lookup=lookup,
            application_request=application_request,
        )

        # MCP termina antes de salir del contexto del sandbox.
        await run_mcp_server(server)


async def _run(
    args: argparse.Namespace,
    catalog: RequirementCatalog,
    operations: list[OperationReference],
) -> None:
    """Runs a new investigation and prints its summary."""
    config = RunConfig(
        working_directory=args.working_directory,
        objective=args.objective,
        settings=args.settings,
        runs_path=args.runs_path,
        checkpoint_path=args.checkpoint_path,
        sandbox=args.sandbox,
        seed_file=args.seed_file,
        headers_file=args.headers_file,
        base_url=getattr(args, "base_url", None),
        review_of=getattr(args, "review_of", None),
    )
    control = RunControl(on_created=lambda run_id: print(f"Investigation: {run_id}", file=sys.stderr))
    summary = await run_investigation(config, catalog, operations, control=control)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    """Validates input and returns the process exit code."""
    args = _build_parser().parse_args(argv)

    try:
        if args.command == "review":
            register_decision(args.runs_path, args.run_id, read_json(args.decision_file),
                              requirements=args.requirements, seed=args.seed_file, settings=args.settings)
            print("Supervisor decision recorded.")

            return 0

        directory = args.working_directory.resolve()

        if not directory.is_dir():
            raise NotADirectoryError(directory)

        args.working_directory = directory
        args.requirements = (directory / args.requirements).resolve()

        if args.command in ("run", "serve"):
            if args.seed_file is not None and not args.sandbox:
                raise ValueError("--seed-file requires --sandbox.")

            if args.sandbox:
                seed = args.seed_file

                if seed is None:
                    seed = Path("sandbox/target_app/seed/products.json")

                args.seed_file = (directory / seed).resolve()

                if not args.seed_file.is_file():
                    raise FileNotFoundError(args.seed_file)

        catalog, operations = prepare_request(args.requirements, args.version, args.operation)

        if args.command == "run":
            asyncio.run(_run(args, catalog, operations))

        else:
            asyncio.run(_serve(args, catalog, operations))

    except KeyboardInterrupt:
        print(
            "Execution interrupted. Operations in progress may have an unknown result.",
            file=sys.stderr,
        )

        return 130

    except Exception as error:
        print(
            f"Error ({type(error).__name__}): {redact(str(error))}",
            file=sys.stderr,
        )

        # Detailed provider exceptions can contain credentials or complete prompts.
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

