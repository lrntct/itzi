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

import os
from pathlib import Path

import itzi.messenger as msgr
from itzi.const import VerbosityLevel


def itzi_cloud_login(cli_args) -> None:
    from itzi.cloud.auth import login, logout, is_logged, get_email
    import getpass

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


def itzi_cloud_push(cli_args) -> None:
    """Pack the input data, then submit a request to the cloud compute provider."""
    from itzi.cloud.push import create_request, request_simulation, upload_input, confirm_upload
    from itzi.cloud.auth import get_token, check_login
    from itzi.cloud.metadata_storage import save_simulation_metadata

    os.environ["ITZI_VERBOSE"] = str(VerbosityLevel.MESSAGE)

    email = check_login()
    session_token = get_token(email)

    for conf_file in cli_args.config_file:
        conf_file_name = Path(conf_file).name
        request_data, input_path, grass_params = create_request(
            cli_args.project, conf_file, force=cli_args.force
        )
        try:
            response_dict = request_simulation(session_token=session_token, metadata=request_data)
            msgr.message(f"{conf_file_name}: Uploading input data...")
            upload_ok = upload_input(
                signed_url=response_dict["upload_url"],
                payload=input_path,
                content_md5=request_data.dataset_hash,
                content_type="application/gzip",
            )
            if upload_ok:
                msgr.message(f"{conf_file_name}: Uploading input data success!")
                # Send upload confirmation to API
                confirm_upload(session_token, response_dict["fingerprint"])
                # Save metadata for later retrieval
                try:
                    save_simulation_metadata(
                        fingerprint=response_dict["fingerprint"],
                        email=email,
                        config_file=str(conf_file),
                        grass_params=grass_params,
                    )
                    msgr.debug(f"Saved metadata for simulation {response_dict['fingerprint']}")
                except Exception as e:
                    msgr.warning(f"Failed to save metadata: {e}")
        except Exception as e:
            msgr.warning(f"{conf_file_name}: Error during cloud submission: {e}")


def itzi_cloud_status(cli_args) -> None:
    """List the requested simulations or display status of a specific simulation."""
    from itzi.cloud.status import get_simulations_list, get_simulation, display_simulations_list
    from itzi.cloud.auth import get_token, check_login

    os.environ["ITZI_VERBOSE"] = str(VerbosityLevel.MESSAGE)

    email = check_login()

    if cli_args.fingerprint:
        # Query single simulation by fingerprint
        task = get_simulation(session_token=get_token(email), fingerprint=cli_args.fingerprint)
        display_simulations_list([task])
    else:
        # List all simulations
        tasks = get_simulations_list(session_token=get_token(email))
        display_simulations_list(tasks)


def itzi_cloud_pull(cli_args) -> None:
    """Retrieve results from the cloud and insert them in the GRASS DB."""
    from itzi.cloud.pull import get_simulation_results_url, pull_simulation_results
    from itzi.cloud.auth import get_token, check_login
    from itzi.cloud.grass_utils import get_active_grass_params
    from itzi.cloud.metadata_storage import load_simulation_metadata
    from itzi.data_containers import GrassParams

    os.environ["ITZI_VERBOSE"] = str(VerbosityLevel.MESSAGE)

    email = check_login()

    msgr.message(f"Retrieving results for simulation {cli_args.fingerprint}...")

    # Determine GRASS parameters with 3-tier priority logic
    grass_params = None
    source_description = None

    # 1. Active GRASS Session (highest priority)
    session_params = get_active_grass_params()
    if session_params is not None:
        grass_params = session_params
        source_description = "active GRASS session"
        msgr.debug("Using GRASS parameters from active session")

    # 2. CLI Arguments (explicit user override)
    elif cli_args.gisdb or cli_args.project or cli_args.mapset:
        # All three must be provided if any are
        if not all([cli_args.gisdb, cli_args.project, cli_args.mapset]):
            msgr.fatal(
                "When specifying GRASS parameters via CLI, all three are required: "
                "--gisdb, --project, and --mapset"
            )
        grass_params = GrassParams(
            grassdata=cli_args.gisdb,
            location=cli_args.project,
            mapset=cli_args.mapset,
            region=None,
            mask=None,
            grass_bin=None,
        )
        source_description = "CLI arguments"
        msgr.debug("Using GRASS parameters from CLI arguments")

    # 3. Stored Metadata
    else:
        metadata_params = load_simulation_metadata(cli_args.fingerprint)
        if metadata_params is not None:
            grass_params = metadata_params
            source_description = "stored metadata"
            msgr.debug("Using GRASS parameters from stored metadata")

    # 4. Error if none available
    if grass_params is None:
        msgr.fatal(
            "Could not determine GRASS parameters for loading results.\n"
            "Please either:\n"
            "  1. Run this command from within a GRASS session, or\n"
            "  2. Specify parameters explicitly: --gisdb <path> --project <name> --mapset <name>\n"
            f"No metadata found for simulation {cli_args.fingerprint}"
        )
        assert False, "Here to narrow down the type and please ty"

    msgr.message(f"Loading results to GRASS database using {source_description}")
    msgr.verbose(
        f"  Location: {grass_params.grassdata}/{grass_params.location}/{grass_params.mapset}"
    )

    # Get download information
    results_info = get_simulation_results_url(
        session_token=get_token(email), fingerprint=cli_args.fingerprint
    )

    # Pull and load the results
    pull_simulation_results(
        download_url=results_info["download_url"],
        grass_params=grass_params,
        overwrite=cli_args.overwrite,
    )
