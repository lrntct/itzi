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
from typing import TYPE_CHECKING, Dict, Mapping
from pathlib import Path
import tempfile
import json
from datetime import datetime, timedelta, timezone

from itzi.grass_session import GrassSessionManager
from itzi.const import TemporalType
import itzi.messenger as msgr

if TYPE_CHECKING:
    from itzi.data_containers import SimulationConfig
    from itzi.configreader import ConfigReader

try:
    import xarray as xr
    import requests
    import keyring
except ImportError:
    raise ImportError(
        "To use the cloud functionalities, install itzi with: "
        "'uv tool install itzi[cloud]' "
        "or 'pip install itzi[cloud]'"
    )


def pack_input(config_reader: ConfigReader):
    """Pack all input data into a netcdf file."""
    sim_config: SimulationConfig = config_reader.get_sim_params()
    grass_params = config_reader.grass_params
    with GrassSessionManager(grass_params):
        cat_dict = list_input_maps(sim_config.input_map_names)
        with tempfile.TemporaryDirectory(prefix="itzi-") as tempdir:
            temp_path = Path(tempdir)
            ds_to_zarr(cat_dict, grass_params, sim_config, tempdir=temp_path)

    sim_config_json = json.dumps(sim_config.as_str_dict())
    print(sim_config_json)


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


def login(url: str, email: str, password: str) -> None:
    """Log into the cloud service.
    Store session token into the system keyring.
    """
    data = {
        "email": str(email),
        "password": str(password),
    }
    with requests.Session() as session:
        response = session.post(url, json=data)
        if response.status_code == 200:
            resp_dict = json.loads(response.text)
        else:
            msgr.fatal(
                f"Authentiction failed. Code: {response.status_code}. Reason: {response.reason}"
            )
    session_token = resp_dict["meta"]["session_token"]
    store_token(email, session_token)
    msgr.message(f"Successfully logged in with account {email}")


def store_token(email: str, token: str, expires_in_days: int = 10) -> None:
    """Store token with expiration timestamp"""
    expiry = datetime.now(timezone.utc) + timedelta(days=expires_in_days)

    token_data = {"token": token, "expires_at": expiry.isoformat()}

    keyring.set_password("itzi_cloud", email, json.dumps(token_data))


def get_valid_token(email: str) -> None | str:
    """Retrieve token. If expired or non-existent, return None"""
    stored = keyring.get_password("itzi_cloud", email)

    if not stored:
        return None

    token_data = json.loads(stored)
    expiry = datetime.fromisoformat(token_data["expires_at"])

    if datetime.now(timezone.utc) >= expiry:
        # Token expired, delete it
        msgr.warning(f"Session for {email} expired, please log again.")
        keyring.delete_password("itzi_cloud", email)
        return None

    return token_data["token"]
