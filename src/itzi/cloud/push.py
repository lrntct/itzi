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
from typing import TYPE_CHECKING, Mapping
from pathlib import Path
import tempfile
import json
import uuid
import tarfile
import hashlib
import base64
from datetime import datetime, timedelta, timezone

import numpy as np
import requests

from itzi.grass_session import GrassSessionManager
from itzi.configreader import ConfigReader
from itzi.const import TemporalType, DefaultValues
from itzi.cloud import urls
from itzi.cloud.schemas import InputInfo, DomainInfo, SimulationRequestSchema
from itzi.cloud.grass_utils import get_grass_params_from_env

if TYPE_CHECKING:
    from itzi.data_containers import SimulationConfig, GrassParams
    from itzi.providers.grass_interface import GrassInterface

try:
    import xarray as xr
except ImportError:
    raise ImportError(
        "To use the cloud functionalities, install itzi with: "
        "'uv tool install itzi[cloud]' "
        "or 'pip install itzi[cloud]'"
    )


def pack_input(sim_config: SimulationConfig, grass_params: GrassParams) -> InputInfo:
    """Pack all input data into a tared zarr."""
    with GrassSessionManager(grass_params):
        from itzi.providers.grass_interface import GrassInterface

        grass_interface = GrassInterface(
            start_time=sim_config.start_time,
            end_time=sim_config.start_time,
            dtype=np.float32,
            region_id=grass_params.region,
            raster_mask_id=grass_params.mask,
        )
        domain_info = DomainInfo(
            rows=grass_interface.yr,
            cols=grass_interface.xr,
            ewres=grass_interface.dx,
            nsres=grass_interface.dy,
        )
        cat_dict = list_input_maps(sim_config.input_map_names, grass_interface)

        with tempfile.TemporaryDirectory(prefix="itzi-") as tempdir:
            temp_path = Path(tempdir)
            temp_path_zarr = temp_path / Path("itzi_input.zarr")
            # Get the zarr
            to_zarr(cat_dict, grass_params, sim_config, tempdir=temp_path_zarr)

            # Create tar.gz file in a temp dir
            temp_dir_path = Path(tempfile.mkdtemp(prefix="itzi-input-"))
            tar_path = temp_dir_path / Path(f"itzi-input-{uuid.uuid4()}.tgz")
            with tarfile.open(name=tar_path, mode="x:gz") as tar_file:
                tar_file.add(temp_path_zarr, arcname="itzi_input.zarr")

    # remove mapset info from all maps in sim_config
    cleaned_input_map_names = {
        key: value.split("@")[0] for key, value in sim_config.input_map_names.items() if value
    }
    cleaned_output_map_names = {
        key: value.split("@")[0] for key, value in sim_config.output_map_names.items() if value
    }
    sim_config = sim_config.model_copy(
        update={
            "input_map_names": cleaned_input_map_names,
            "output_map_names": cleaned_output_map_names,
        }
    )

    return InputInfo(
        sim_config=sim_config,
        dataset_path=tar_path,
        dataset_hash=md5_base64(tar_path),
        dataset_bytes=tar_path.stat().st_size,
        domain_info=domain_info,
    )


def extract_dimension_names(ds: xr.Dataset) -> dict[str, dict[str, str]]:
    """Extract dimension names for each variable in the dataset.

    Returns a dictionary mapping variable names to their dimension mappings.
    Assumes that:
    - x and y dimensions are named 'x' and 'y'.
    - Time dimension follows pattern 'start_time_<varname>'.
    """
    dimension_names: dict[str, dict[str, str]] = {}

    for var_name in ds.data_vars:
        da: xr.DataArray = ds[var_name]
        var_dims: dict[str, str] = {"x": "x", "y": "y"}

        # Check for time dimension
        expected_time_dim = f"start_time_{var_name}"
        if expected_time_dim in da.dims:
            var_dims["time"] = expected_time_dim

        dimension_names[str(var_name)] = var_dims

    return dimension_names


def validate_dimension_conventions(ds: xr.Dataset) -> None:
    """Validate that the dataset follows expected dimension naming conventions.

    Raises ValueError if:
    - x or y coordinates are missing
    - A 3D variable's time dimension doesn't follow the start_time_<varname> pattern

    Parameters
    ----------
    ds : xr.Dataset
        The dataset to validate.
    """
    # Check for x and y coordinates
    if "x" not in ds.coords:
        raise ValueError("Dataset missing expected 'x' coordinate")
    if "y" not in ds.coords:
        raise ValueError("Dataset missing expected 'y' coordinate")

    # Validate time dimension naming for 3D variables
    for var_name in ds.data_vars:
        da: xr.DataArray = ds[var_name]
        if len(da.dims) == 3:
            expected_time_dim = f"start_time_{var_name}"
            if expected_time_dim not in da.dims:
                raise ValueError(
                    f"3D variable '{var_name}' has unexpected dimensions {list(da.dims)}. "
                    f"Expected time dimension '{expected_time_dim}'"
                )


def to_zarr(
    cat_dict: Mapping[str, dict[str, list[str]]],
    grass_params: GrassParams,
    sim_config: SimulationConfig,
    tempdir: Path,
) -> None:
    """Read the itzi input from GRASS as an xr.Dataset.
    Select the correct time period.
    Write the Dataset to a temporary zarr store."""

    ds = read_all_maps(cat_dict, grass_params)

    if sim_config.temporal_type == TemporalType.RELATIVE:
        start_time = timedelta(seconds=0)
        end_time = sim_config.end_time - sim_config.start_time
    else:
        start_time = sim_config.start_time
        end_time = sim_config.end_time

    time_coords = []
    for coords_name, coords_values in ds.coords.items():
        if "start_time" in str(coords_name):
            time_coords.append(coords_name)
    time_slices = {tc: slice(start_time, end_time) for tc in time_coords}
    ds_select = ds.sel(**time_slices)

    # Validate and extract dimension names
    validate_dimension_conventions(ds_select)
    dimension_names = extract_dimension_names(ds_select)

    # Add dimension names to dataset attributes
    ds_select.attrs["itzi_dimension_names"] = dimension_names

    ds_select.to_zarr(tempdir)


def read_all_maps(
    cat_dict: Mapping[str, dict[str, list[str]]], grass_params: GrassParams
) -> xr.Dataset:
    dataset_list = []
    for mapset, map_dict in cat_dict.items():
        grass_db = Path(grass_params.grassdata)
        grass_project = grass_db / Path(grass_params.location)
        mapset_path = grass_project / Path(mapset)
        ds = xr.open_dataset(mapset_path, backend_kwargs=map_dict)
        dataset_list.append(ds)
    return xr.merge(dataset_list, compat="identical", join="exact")


def list_input_maps(
    input_map_names: Mapping[str, str | None], grass_interface: GrassInterface
) -> dict[str, dict[str, list[str]]]:
    """Create a dict of map name lists categorized by mapset and type (raster or strds)"""

    categorized: dict[str, dict[str, list[str]]] = {}
    for map_key, map_name in input_map_names.items():
        if map_name:
            map_id = grass_interface.format_id(map_name)
            mapset = map_id.split("@", 1)[1]
            if mapset not in categorized:
                categorized[mapset] = {"raster": [], "strds": []}
            if grass_interface.name_is_stds(map_id):
                categorized[mapset]["strds"].append(map_id)
            elif grass_interface.name_is_map(map_id):
                categorized[mapset]["raster"].append(map_id)
            else:
                raise ValueError(f"Input map <{map_id}> not found in GRASS database.")
    # Add the raster mask from the current mapset
    if grass_interface.has_mask():
        current_mapset = grass_interface.get_current_mapset()
        if current_mapset not in categorized:
            categorized[current_mapset] = {"raster": [], "strds": []}
        categorized[current_mapset]["raster"].append(f"MASK@{current_mapset}")
    return categorized


def create_request(
    email: str | None, conf_file_path: str | Path
) -> tuple[SimulationRequestSchema, Path, GrassParams]:
    """Create a simulation request.

    Detects active GRASS session and merges with config file parameters.

    Parameters
    ----------
    email : str
        User email address.
    conf_file_path : str | Path
        Path to the configuration file.

    Returns
    -------
    Tuple[Dict, Path, GrassParams]
        Request data dictionary, input dataset path, and the GRASS parameters used.
    """

    config_reader = ConfigReader(conf_file_path)
    # Pack the input
    sim_config: SimulationConfig = config_reader.get_sim_params()

    # Get GRASS params with session detection and priority logic
    config_grass_params = config_reader.get_grass_params()
    grass_params, _source = get_grass_params_from_env(config_grass_params)

    input_info = pack_input(sim_config, grass_params)

    # Unique request identifier (email + datetime + config + input_hash) with blake2b and 8 bytes digest
    config_json = json.dumps(input_info.sim_config.model_dump(mode="json"))
    fingerprint_source = (
        f"{email}{datetime.now(timezone.utc)}{config_json}{input_info.dataset_hash}"
    )
    request_fingerprint = hashlib.blake2b(
        fingerprint_source.encode("utf-8"), digest_size=8
    ).hexdigest()

    # Estimation of Lattice updates
    estimated_ts = estimate_timesteps(
        domain_info=input_info.domain_info, sim_config=input_info.sim_config
    )

    request_data = SimulationRequestSchema(
        fingerprint=request_fingerprint,
        estimated_timesteps=estimated_ts,
        sim_config=input_info.sim_config,
        dataset_hash=input_info.dataset_hash,
        dataset_bytes=input_info.dataset_bytes,
        domain_info=input_info.domain_info,
    )
    return request_data, input_info.dataset_path, grass_params


def estimate_timesteps(domain_info: DomainInfo, sim_config: SimulationConfig) -> int:
    """Estimate the number of time steps necessary to complete the simulation."""
    from itzi.surfaceflow import SurfaceFlowSimulation

    min_dim: float = min(domain_info.ewres, domain_info.nsres)  # metres

    time_step_duration: float = SurfaceFlowSimulation.dt_s(
        min_dim=min_dim,
        g=DefaultValues.G,
        # Values to infer a conservative step duration
        maxh=10,
        cfl=0.5,
    )
    sim_duration: float = (sim_config.end_time - sim_config.start_time).total_seconds()
    return int(sim_duration / time_step_duration)


def blake2b_hex(file_path: Path, digest_size: int = 64) -> str:
    """Return the hex digest of a file."""
    hasher = hashlib.blake2b(digest_size=digest_size)
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def md5_base64(file_path: Path) -> str:
    """Calculates the Base64-encoded MD5 digest."""
    md5_hasher = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            md5_hasher.update(chunk)
    md5_hash = md5_hasher.digest()  # Get the binary digest
    md5_base64 = base64.b64encode(md5_hash).decode("utf-8")  # Base64 encode the binary digest
    return md5_base64


def request_simulation(
    session_token: str,
    metadata: SimulationRequestSchema,
    endpoint: str = urls.SIMULATIONS_ENDPOINT,
) -> dict[str, str]:
    """Send simulation metadata. Return the URL for upload."""
    headers: dict[str, str] = {"X-Session-Token": session_token}
    with requests.Session() as session:
        response = session.post(endpoint, json=metadata.model_dump(mode="json"), headers=headers)
    if response.status_code == 201:
        return json.loads(response._content)
    else:
        raise RuntimeError(f"Something went wrong: {response}")


def upload_input(signed_url: str, payload: Path, content_md5: str, content_type: str) -> bool:
    headers: dict[str, str] = {"content-md5": content_md5, "content-type": content_type}
    with requests.Session() as session:
        with open(payload, mode="rb") as data:
            response = session.put(signed_url, data=data, headers=headers)
    if response.status_code == 200:
        # return json.loads(response._content)
        return True
    else:
        raise RuntimeError(f"Something went wrong: {response.__dict__}")
