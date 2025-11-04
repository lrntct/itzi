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
from typing import TYPE_CHECKING, Dict, Mapping, Tuple
from pathlib import Path
import tempfile
import json
import tarfile
import hashlib
from datetime import datetime, timedelta, timezone

from itzi.grass_session import GrassSessionManager
from itzi.configreader import ConfigReader
from itzi.const import TemporalType
import itzi.messenger as msgr

if TYPE_CHECKING:
    from itzi.data_containers import SimulationConfig
    from itzi.configreader import ConfigReader

try:
    import xarray as xr
except ImportError:
    raise ImportError(
        "To use the cloud functionalities, install itzi with: "
        "'uv tool install itzi[cloud]' "
        "or 'pip install itzi[cloud]'"
    )


def pack_input(config_reader: ConfigReader) -> Tuple[Path, str]:
    """Pack all input data into a netcdf file."""
    sim_config: SimulationConfig = config_reader.get_sim_params()
    sim_config_json = json.dumps(sim_config.as_str_dict())
    grass_params = config_reader.grass_params
    with GrassSessionManager(grass_params):
        cat_dict = list_input_maps(sim_config.input_map_names)
        with tempfile.TemporaryDirectory(prefix="itzi-") as tempdir:
            temp_path = Path(tempdir)
            temp_path_zarr = temp_path / Path("itzi_input.zarr")
            # Get the zarr
            ds_to_zarr(cat_dict, grass_params, sim_config, tempdir=temp_path_zarr)

            # Create tar.gz file in a temp dir
            temp_dir_path = Path(tempfile.mkdtemp(prefix="itzi-input-"))
            now = datetime.now(timezone.utc)
            tar_path = temp_dir_path / Path(f"itzi-input{now}.tgz")
            with tarfile.open(name=tar_path, mode="x:gz") as tar_file:
                tar_file.add(temp_path_zarr, arcname="itzi_input.zarr")
    return tar_path, sim_config_json


def ds_to_zarr(
    cat_dict: Mapping, grass_params: Mapping, sim_config: SimulationConfig, tempdir: Path
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
        if "start_time" in coords_name:
            time_coords.append(coords_name)
    time_slices = {tc: slice(start_time, end_time) for tc in time_coords}
    ds_select = ds.sel(**time_slices)
    ds_select.to_zarr(tempdir)


def read_all_maps(cat_dict: Mapping, grass_params: Mapping) -> xr.Dataset:
    dataset_list = []
    for mapset, map_dict in cat_dict.items():
        grass_db = Path(grass_params["grassdata"])
        grass_project = grass_db / Path(grass_params["location"])
        mapset_path = grass_project / Path(mapset)
        ds = xr.open_dataset(mapset_path, **map_dict)
        dataset_list.append(ds)
    return xr.merge(dataset_list, compat="identical", join="exact")


def list_input_maps(input_map_names: Mapping) -> Dict[Dict[str : [str, ...]]]:
    """Create a dict of map name lists categorized by mapset and type (raster or strds)"""
    from itzi.providers.grass_interface import GrassInterface

    categorized = {}
    for map_key, map_name in input_map_names.items():
        if map_name:
            map_id = GrassInterface.format_id(map_name)
            mapset = map_id.split("@", 1)[1]
            if mapset not in categorized:
                categorized[mapset] = {"raster": [], "strds": []}
            if GrassInterface.name_is_stds(map_id):
                categorized[mapset]["strds"].append(map_id)
            elif GrassInterface.name_is_map(map_id):
                categorized[mapset]["raster"].append(map_id)
            else:
                raise ValueError(f"Input map <{map_id}> not found in GRASS database.")
    # Add the raster mask from the current mapset
    if GrassInterface.has_mask():
        current_mapset = GrassInterface.get_current_mapset()
        if current_mapset not in categorized:
            categorized[current_mapset] = {"raster": [], "strds": []}
        categorized[current_mapset]["raster"].append(f"MASK@{current_mapset}")
    return categorized


def create_request(email: str, conf_file_path: str | Path):
    conf_file_name = Path(conf_file_path).name
    msgr.message(f"Packing input data for {conf_file_name}...")
    config_reader = ConfigReader(conf_file_path)
    tar_path, config_json = pack_input(config_reader)
    print(tar_path)

    hash_tar = blake2b(tar_path)
    print(hash_tar)
    # Unique request identifier (email + datetime + config + input_hash) with blake2b and 8 bytes digest
    fingerprint_source = f"{email}{datetime.now(timezone.utc)}{config_json}{hash_tar}"
    request_fingerprint = hashlib.blake2b(
        fingerprint_source.encode("utf-8"), digest_size=8
    ).hexdigest()
    print(request_fingerprint)


def blake2b(file_path: str, digest_size: int = 64):
    """Return the hex digest of a file."""
    hasher = hashlib.blake2b(digest_size=digest_size)
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hasher.update(chunk)
    return hasher.hexdigest()
