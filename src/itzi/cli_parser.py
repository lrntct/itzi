"""
Copyright (C) 2025-2026 Laurent Courty

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
"""

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


def add_cloud_subcommands(cloud_parser: argparse.ArgumentParser) -> None:
    cloud_subparser = cloud_parser.add_subparsers(dest="cloud_command")

    cloud_login_parser = cloud_subparser.add_parser("login", help="Login to the cloud provider.")
    cloud_login_parser.set_defaults(cloud_handler="login")
    cloud_login_parser.add_argument("--email", help="Account email.")
    cloud_login_parser.add_argument("--password", help="Account password.")
    cloud_login_parser.add_argument(
        "-o", "--logout", action="store_true", help="Log out from the current session."
    )
    cloud_login_parser.add_argument(
        "-s", "--status", action="store_true", help="Get session status."
    )

    cloud_push_parser = cloud_subparser.add_parser(
        "push", help="Submit ensembles for cloud processing from YAML files"
    )
    cloud_push_parser.set_defaults(cloud_handler="push")
    cloud_push_parser.add_argument(
        "-p",
        "--project",
        required=True,
        metavar="PROJECT_ID",
        help="Project ID (from cloud project --list).",
    )
    cloud_push_parser.add_argument(
        "config_file",
        nargs="+",
        help="One or more .yaml ensemble configuration files.",
    )
    cloud_push_parser.add_argument(
        "--force",
        action="store_true",
        help="Create a new Ensemble when the YAML is unchanged but its Input archive has changed.",
    )

    cloud_status_parser = cloud_subparser.add_parser(
        "status", help="Display Input states and recorded Run IDs for local cloud submissions."
    )
    cloud_status_parser.set_defaults(cloud_handler="status")
    cloud_status_parser.add_argument(
        "ensemble_id",
        nargs="?",
        metavar="ENSEMBLE_ID",
        help="Optional locally recorded Ensemble ID; defaults to submissions for this account.",
    )

    cloud_project_parser = cloud_subparser.add_parser("project", help="Manage cloud projects.")
    cloud_project_parser.set_defaults(cloud_handler="project")
    cloud_project_parser.add_argument(
        "-l",
        "--list",
        action="store_true",
        required=True,
        help="List projects available to the authenticated user.",
    )

    cloud_pull_parser = cloud_subparser.add_parser(
        "pull",
        help="Download a verified result package for selected cloud Runs.",
        description=(
            "Download a verified result package. GRASS import requires a published "
            "result-package format and is not yet available."
        ),
    )
    cloud_pull_parser.set_defaults(cloud_handler="pull")
    cloud_pull_parser.add_argument(
        "ensemble_id",
        metavar="ENSEMBLE_ID",
        help="Ensemble ID whose results should be packaged.",
    )
    cloud_pull_parser.add_argument(
        "--run-id",
        action="append",
        default=[],
        help="Select a Run ID. Repeat for 1–100 runs; defaults to locally recorded runs.",
    )
    cloud_pull_parser.add_argument(
        "-o",
        "--output",
        required=True,
        help="Destination archive file (must not already exist).",
    )


def build_cloud_parser(prog: str | None = None) -> argparse.ArgumentParser:
    cloud_parser = argparse.ArgumentParser(
        prog=prog,
        description="Run and manage cloud simulations.",
    )
    add_cloud_subcommands(cloud_parser)
    return cloud_parser


def print_cloud_help(_cli_args: argparse.Namespace | None = None) -> None:
    build_cloud_parser(prog=f"{build_parser().prog} cloud").print_help()


def build_parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser(description=DESCR)
    subparsers = arg_parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a configuration file or YAML stream.")
    run_parser.add_argument(
        "config_file",
        nargs="+",
        help="An Itzï configuration file. ini and yaml are allowed, several files form one batch.",
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
            "For batch processing, use CONFIG_PATH=HOTSTART_PATH."
        ),
    )
    run_parser.add_argument(
        "--member",
        action="append",
        default=[],
        metavar="[ENSEMBLE#]SIMULATION",
        help="Run only one ensemble member. Repeat to select multiple members.",
    )
    run_parser.add_argument(
        "-d",
        "--dry-run",
        dest="dry",
        action="store_true",
        help="Resolve and validate selected members without writing user artifacts.",
    )

    # display version
    subparsers.add_parser("version", help="Display software version number.")

    # Cloud functions subparser
    cloud_parser = subparsers.add_parser("cloud", help="Run and manage cloud simulations.")
    cloud_parser.set_defaults(cloud_handler="help")
    add_cloud_subcommands(cloud_parser)

    return arg_parser
