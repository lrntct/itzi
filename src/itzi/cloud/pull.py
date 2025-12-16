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
from typing import TYPE_CHECKING
import json
import tarfile
import tempfile
import uuid
from pathlib import Path

import itzi.messenger as msgr
from itzi.cloud import urls
from itzi.grass_session import GrassSessionManager

if TYPE_CHECKING:
    from itzi.data_containers import GrassParams

try:
    import requests
    import xarray as xr
except ImportError:
    raise ImportError(
        "To use the cloud functionalities, install itzi with: "
        "'uv tool install itzi[cloud]' "
        "or 'pip install itzi[cloud]'"
    )


def get_simulation_results_url(
    session_token: str, fingerprint: str, endpoint: str = urls.SIMULATIONS_ENDPOINT
) -> dict:
    """Get the results download information for a simulation."""
    headers = {"X-Session-Token": session_token}
    results_url = f"{endpoint}/{fingerprint}/results"

    with requests.Session() as session:
        response = session.get(results_url, headers=headers)

        if response.status_code != 200:
            # Try to get detailed error from response body
            try:
                error_details = json.loads(response.text)
                # Check if there's a 'detail' field (common in Django/DRF APIs)
                if "detail" in error_details:
                    error_msg = f"Failed to retrieve simulation results: {error_details['detail']}"
                else:
                    # Fall back to full response if no detail field
                    error_msg = "Failed to retrieve simulation results.\n"
                    error_msg += f"Status Code: {response.status_code}\n"
                    error_msg += f"API Response: {json.dumps(error_details, indent=2)}"
            except (json.JSONDecodeError, ValueError):
                # If response is not JSON, show basic error info
                error_msg = "Failed to retrieve simulation results.\n"
                error_msg += f"Status Code: {response.status_code}\n"
                error_msg += f"Reason: {response.reason}\n"
                error_msg += f"Response Text: {response.text}"

            msgr.fatal(error_msg)

        response_data = json.loads(response.text)

    return response_data


def download_results(download_url: str, temp_dir: Path) -> Path:
    """Download simulation results from the signed URL.

    Args:
        download_url: Signed URL to download results from
        temp_dir: Temporary directory to download results to

    Returns:
        Path to the downloaded file
    """
    with requests.Session() as session:
        response = session.get(download_url, stream=True)

        if response.status_code != 200:
            msgr.fatal(
                "Failed to download results. "
                f"Code: {response.status_code}. Reason: {response.reason}"
            )

        # Save to temporary file
        temp_file = temp_dir / Path(f"{uuid.uuid4()}.tgz")
        with open(temp_file, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)

    return temp_file


def load_to_grass(temp_data_path: Path, grass_params: GrassParams) -> None:
    """Load simulation results into GRASS database.

    Parameters
    ----------
    temp_data_path : Path
        Path to the downloaded results data.
    grass_params : GrassParams
        GRASS parameters specifying where to load the results.
    """
    ds_results = xr.open_zarr(temp_data_path)
    msgr.message("Results dataset summary:")
    print(ds_results)

    # map time dimension of all strds
    time_mapping = {"start_time": "time"}
    dims_mapping = {}
    for var, da in ds_results.data_vars.items():
        if "time" in da.coords.keys():
            dims_mapping[var] = time_mapping
    print(dims_mapping)

    with GrassSessionManager(grass_params):
        # xarray_grass is imported here because it needs an active grass session
        from xarray_grass import to_grass

        to_grass(ds_results, dims=dims_mapping)

    msgr.message(f"Maximum water depth: {ds_results['itzi_demo_water_depth'].max().compute()}")
    msgr.message(
        f"Results will be loaded to: {grass_params.grassdata}/{grass_params.location}/{grass_params.mapset}"
    )


def pull_simulation_results(download_url: str, grass_params: GrassParams) -> None:
    """Pull simulation results from the cloud and load them into GRASS.

    Parameters
    ----------
    download_url : str
        Signed URL to download results from.
    grass_params : GrassParams
        GRASS parameters specifying where to load the results.
    """
    # Create temporary directory for download
    with tempfile.TemporaryDirectory(prefix="itzi-results-") as temp_dir:
        temp_path = Path(temp_dir)

        # Download the results
        msgr.message("Downloading results...")
        downloaded_file = download_results(download_url, temp_path)

        msgr.message(f"Downloaded to {downloaded_file}")

        # Extract the tgz file
        msgr.message("Extracting results...")
        extract_dir = temp_path / Path("extracted")
        extract_dir.mkdir(exist_ok=True)

        with tarfile.open(downloaded_file, "r:gz") as tar:
            tar.extractall(path=extract_dir, filter="data")

        # Load results into GRASS
        msgr.message("Loading results into GRASS...")
        load_to_grass(extract_dir / Path("results.zarr"), grass_params)

    msgr.message("Results successfully retrieved!")
