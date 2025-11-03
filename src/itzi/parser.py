"""parse command line"""

from __future__ import annotations

import argparse


DESCR = "A dynamic, fully distributed hydraulic and hydrologic model."


def parse_resume_from(arg_value: str) -> tuple[str | None, str]:
    if "=" not in arg_value:
        return None, arg_value

    key, path = arg_value.split("=", 1)
    if not key or not path:
        raise argparse.ArgumentTypeError(
            "--resume-from must be HOTSTART_PATH or CONFIG_PATH=HOTSTART_PATH"
        )
    return key, path


def build_parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser(description=DESCR)
    subparsers = arg_parser.add_subparsers(dest="command", required=True)

    # run a simulation locally
    run_parser = subparsers.add_parser("run", help="Run a simulation.")
    run_parser.add_argument(
        "config_file",
        nargs="+",
        help=("An Itzï configuration file (if several given, run in batch mode.)"),
    )
    run_parser.add_argument("-o", action="store_true", help="Overwrite files if exist.")
    verbosity_parser = run_parser.add_mutually_exclusive_group()
    verbosity_parser.add_argument("-v", action="count", help="Increase verbosity.")
    verbosity_parser.add_argument("-q", action="count", help="Decrease verbosity.")
    run_parser.add_argument(
        "--resume-from",
        action="append",
        type=parse_resume_from,
        metavar="HOTSTART_PATH | CONFIG_PATH=HOTSTART_PATH",
        default=[],
        help=(
            "Resume a simulation from a hotstart file. "
            "If only the path to a hotstart file is given, batch is not allowed. "
            "For batch processing, use the 'CONFIG_PATH=HOTSTART_PATH' construct."
        ),
    )

    # display version
    subparsers.add_parser("version", help="Display software version number.")

    # Cloud functions subparser
    cloud_parser = subparsers.add_parser("cloud", help="Run and manage cloud simulations.")
    cloud_subparser = cloud_parser.add_subparsers(dest="cloud_command", required=True)

    cloud_login_parser = cloud_subparser.add_parser("login", help="Login to the cloud provider.")
    cloud_login_parser.add_argument("--email", help="Account email.")
    cloud_login_parser.add_argument("--password", help="Account password.")

    cloud_push_parser = cloud_subparser.add_parser(
        "push", help="submit a simulation to run in the cloud"
    )
    cloud_push_parser.add_argument(
        "config_file",
        nargs="+",
        help=("An Itzï configuration file (if several given, run in batch mode.)"),
    )

    cloud_status_parser = cloud_subparser.add_parser(
        "status", help="Display status of submitted cloud simulations."
    )

    cloud_pull_parser = cloud_subparser.add_parser(
        "pull", help="Pull results from a complete cloud simulation."
    )

    return arg_parser
