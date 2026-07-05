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
import gzip
import hashlib
import base64
from datetime import timedelta

import numpy as np
import requests

from itzi.grass_session import GrassSessionManager
from itzi.configreader import ConfigReader
from itzi.const import TemporalType
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


def _normalize_tar_info(tar_info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Normalize tar member metadata so identical inputs produce identical archives."""

    tar_info.mtime = 0
    tar_info.uid = 0
    tar_info.gid = 0
    tar_info.uname = ""
    tar_info.gname = ""
    tar_info.mode = 0o755 if tar_info.isdir() else 0o644
    return tar_info


def _add_to_tar(tar_file: tarfile.TarFile, source_path: Path, arcname: str) -> None:
    """Add files to a tar archive in a stable order with normalized metadata."""

    tar_info = tar_file.gettarinfo(str(source_path), arcname)
    tar_info = _normalize_tar_info(tar_info)

    if source_path.is_dir():
        tar_file.addfile(tar_info)
        for child_path in sorted(source_path.iterdir(), key=lambda path: path.name):
            _add_to_tar(tar_file, child_path, f"{arcname}/{child_path.name}")
        return

    with open(source_path, "rb") as source_file:
        tar_file.addfile(tar_info, fileobj=source_file)


def _write_reproducible_tar_gz(source_path: Path, tar_path: Path, arcname: str) -> None:
    """Write a tar.gz archive whose bytes are reproducible for identical input trees."""

    with open(tar_path, "xb") as raw_file:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_file, mtime=0) as gzip_file:
            with tarfile.open(fileobj=gzip_file, mode="w") as tar_file:
                _add_to_tar(tar_file, source_path, arcname)


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
            _write_reproducible_tar_gz(temp_path_zarr, tar_path, arcname="itzi_input.zarr")

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

    # Remove history because it contains the creation time and changes the hash
    del ds_select.attrs["history"]

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
    project_id: int, conf_file_path: str | Path, force: bool = False
) -> tuple[SimulationRequestSchema, Path, GrassParams]:
    """Create a simulation request.

    Detects active GRASS session and merges with config file parameters.

    Parameters
    ----------
    project_id : int
        Cloud project ID.
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

    request_data = SimulationRequestSchema(
        project_id=project_id,
        force_rerun=force,
        sim_config=input_info.sim_config,
        dataset_hash=input_info.dataset_hash,
        dataset_bytes=input_info.dataset_bytes,
        domain_info=input_info.domain_info,
    )
    return request_data, input_info.dataset_path, grass_params


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
    endpoint: str | None = None,
) -> dict[str, str]:
    """Send simulation metadata. Return the URL for upload."""
    endpoint = endpoint or urls.get_simulations_endpoint()
    headers: dict[str, str] = {"X-Session-Token": session_token}
    with requests.Session() as session:
        response = session.post(endpoint, json=metadata.model_dump(mode="json"), headers=headers)
    if response.status_code == 201:
        return json.loads(response._content)
    elif response.status_code == 409:
        response_data = json.loads(response.text)
        raise RuntimeError(
            "An identical simulation is already in progress. "
            f"Fingerprint: {response_data['existing_fingerprint']}, "
            f"status: {response_data['status']}."
        )
    else:
        raise RuntimeError(f"Something went wrong: {response}")


def upload_input(signed_url: str, payload: Path, content_md5: str, content_type: str) -> bool:
    headers: dict[str, str] = {"content-md5": content_md5, "content-type": content_type}
    with requests.Session() as session:
        with open(payload, mode="rb") as data:
            response = session.put(signed_url, data=data, headers=headers)
    if response.status_code == 200:
        return True
    else:
        raise RuntimeError(f"Something went wrong: {response.__dict__}")


def confirm_upload(
    session_token: str,
    fingerprint: str,
    endpoint: str | None = None,
) -> bool:
    """Send simulation metadata. Return the URL for upload."""
    endpoint = endpoint or urls.get_simulations_endpoint()
    headers: dict[str, str] = {"X-Session-Token": session_token}
    confirmation_url = f"{endpoint}/{fingerprint}/confirm-upload"
    with requests.Session() as session:
        response = session.post(confirmation_url, headers=headers)
    if response.status_code == 202:
        return True
    else:
        raise RuntimeError(f"Something went wrong: {response}")
