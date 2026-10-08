"""
Copyright (C) 2025 Laurent Courty

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
import os
from pathlib import Path

import itzi.messenger as msgr
from itzi.messenger import VerbosityLevel


def itzi_cloud_login(cli_args) -> None:
    import getpass

    from itzi.cloud.auth import get_email, is_logged, login, logout

    email = get_email(cli_args.email)

    if cli_args.status:
        # Check status
        if is_logged(email=email):
            msgr.message(f"{email} IS authenticated.")
        else:
            msgr.message(f"{email} NOT authenticated.")
        return

    if cli_args.logout:
        # log out
        logout(email=email)
    else:
        # log in
        password = (
            cli_args.password if cli_args.password else getpass.getpass(f"{email}'s password: ")
        )
        login(email=email, password=password)


def itzi_cloud_push(cli_args: argparse.Namespace) -> None:
    """Build Input archives and request one cloud Run per accepted member."""
    os.environ["ITZI_VERBOSE"] = str(VerbosityLevel.MESSAGE)

    from itzi.cloud.auth import check_login, get_token

    email = check_login()
    session_token = get_token(email)
    from itzi.cloud.archive import build_archives
    from itzi.cloud.push import create_ensemble, launch_runs, upload_input, wait_for_input

    for conf_file in cli_args.config_file:
        try:
            for archive in build_archives(conf_file, session_token):
                draft = create_ensemble(
                    archive,
                    project_id=cli_args.project,
                    email=email,
                    session_token=session_token,
                    force=getattr(cli_args, "force", False),
                )
                draft = upload_input(archive, draft, session_token)
                draft = wait_for_input(draft, session_token)
                draft = launch_runs(archive, draft, session_token)
                runs = (f"{index}={run}" for index, run in sorted(draft.run_ids.items()))
                source = archive.ensemble.source
                msgr.message(
                    f"{source.path} document {source.document_index}: "
                    f"Ensemble {draft.ensemble_id}, Input {draft.input_id} accepted; "
                    f"runs: {', '.join(runs)}"
                )
        except Exception as error:  # noqa: BLE001 - surface resolver, reader and HTTP errors.
            msgr.fatal(f"{conf_file}: cloud push failed: {error}")


def itzi_cloud_status(cli_args: argparse.Namespace) -> None:
    """List cloud submissions, or show detailed status for one Ensemble."""
    import requests

    from itzi.cloud.auth import check_login, get_token
    from itzi.cloud.status import (
        display_ensemble_statuses,
        display_ensemble_table,
        get_ensemble_statuses,
    )

    os.environ["ITZI_VERBOSE"] = str(VerbosityLevel.MESSAGE)

    email = check_login()

    try:
        statuses = get_ensemble_statuses(
            session_token=get_token(email), email=email, ensemble_id=cli_args.ensemble_id
        )
    except (requests.RequestException, ValueError, OSError) as error:
        msgr.fatal(f"Cloud status failed: {error}")
    if cli_args.ensemble_id is None:
        display_ensemble_table(statuses)
    else:
        display_ensemble_statuses(statuses)


def itzi_cloud_project(cli_args: argparse.Namespace) -> None:
    """Manage cloud projects."""
    from itzi.cloud.auth import check_login, get_token
    from itzi.cloud.project import display_projects_list, get_projects_list

    os.environ["ITZI_VERBOSE"] = str(VerbosityLevel.MESSAGE)

    if not cli_args.list:
        msgr.fatal("No project action specified. Use --list.")

    email = check_login()
    projects = get_projects_list(session_token=get_token(email))
    display_projects_list(projects)


def itzi_cloud_pull(cli_args: argparse.Namespace) -> None:
    """Request and download a verified result package for an Ensemble's Runs."""
    import requests

    from itzi.cloud.auth import check_login, get_token
    from itzi.cloud.pull import pull_result_package

    os.environ["ITZI_VERBOSE"] = str(VerbosityLevel.MESSAGE)

    email = check_login()

    try:
        path = pull_result_package(
            session_token=get_token(email),
            email=email,
            ensemble_id=cli_args.ensemble_id,
            run_ids=tuple(cli_args.run_id),
            destination=Path(cli_args.output),
        )
    except (requests.RequestException, ValueError, OSError) as error:
        msgr.fatal(f"Cloud pull failed: {error}")
    msgr.message(f"Verified result package saved to {path}")
