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
import dataclasses
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

if TYPE_CHECKING:
    from itzi.data_containers import SimulationConfig
    from itzi.configreader import ConfigReader
    from itzi.providers.grass_interface import GrassInterface

try:
    import xarray as xr
except ImportError:
    raise ImportError(
        "To use the cloud functionalities, install itzi with: "
        "'uv tool install itzi[cloud]' "
        "or 'pip install itzi[cloud]'"
    )


@dataclasses.dataclass(frozen=True)
class DomainInfo:
    rows: int
    cols: int
    ewres: float
    nsres: float


@dataclasses.dataclass(frozen=True)
class InputInfo:
    """Store information about input data."""

    sim_config: SimulationConfig
    dataset_path: Path  # Path to the tgz file containing the zarr of input maps
    dataset_hash: str  # Base64 MD5 of the dataset
    dataset_bytes: int
    domain_info: DomainInfo


def pack_input(config_reader: ConfigReader) -> InputInfo:
    """Pack all input data into a netcdf file."""
    sim_config: SimulationConfig = config_reader.get_sim_params()
    grass_params = config_reader.grass_params
    with GrassSessionManager(grass_params):
        from itzi.providers.grass_interface import GrassInterface

        grass_interface = GrassInterface(
            start_time=sim_config.start_time,
            end_time=sim_config.start_time,
            dtype=np.float32,
            region_id=grass_params["region"],
            raster_mask_id=grass_params["mask"],
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

    return InputInfo(
        sim_config=sim_config,
        dataset_path=tar_path,
        dataset_hash=md5_base64(tar_path),
        dataset_bytes=tar_path.stat().st_size,
        domain_info=domain_info,
    )


def to_zarr(
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


def list_input_maps(
    input_map_names: Mapping, grass_interface: GrassInterface
) -> Dict[Dict[str : [str, ...]]]:
    """Create a dict of map name lists categorized by mapset and type (raster or strds)"""

    categorized = {}
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


def create_request(email: str, conf_file_path: str | Path) -> Tuple[Dict, Path]:
    """"""
    config_reader = ConfigReader(conf_file_path)
    # Pack the input
    input_info = pack_input(config_reader)

    # Unique request identifier (email + datetime + config + input_hash) with blake2b and 8 bytes digest
    config_json = json.dumps(input_info.sim_config.as_str_dict())
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

    request_data = {
        "fingerprint": request_fingerprint,
        "estimated_timesteps": estimated_ts,
        "sim_config": input_info.sim_config.as_str_dict(),
        "dataset_hash": input_info.dataset_hash,
        "dataset_bytes": input_info.dataset_bytes,
        "domain_info": dataclasses.asdict(input_info.domain_info),
    }
    return request_data, input_info.dataset_path


def estimate_timesteps(domain_info: DomainInfo, sim_config: SimulationConfig) -> int:
    """Estimate the number of time steps necessary."""
    from itzi.surfaceflow import SurfaceFlowSimulation

    cfl = 0.5
    maxh = 10  # metres
    g = DefaultValues.G
    min_dim = min(domain_info.ewres, domain_info.nsres)  # metres

    dt = SurfaceFlowSimulation.dt_s(cfl, min_dim, g, maxh)
    duration = (sim_config.end_time - sim_config.start_time).total_seconds()
    return int(duration / dt)


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
    session_token: str, metadata: Mapping, url: str = urls.PUSH_ENDPOINT
) -> Dict:
    """Send simulation metadata. Return the URL for upload."""
    headers = {"X-Session-Token": session_token}
    with requests.Session() as session:
        response = session.post(url, json=metadata, headers=headers)
    if response.status_code == 200:
        return json.loads(response._content)
    else:
        raise RuntimeError(f"Something went wrong: {response}")


def upload_input(signed_url: str, payload: Path, content_md5: str, content_type: str) -> bool:
    headers = {"content-md5": content_md5, "content-type": content_type}
    with requests.Session() as session:
        with open(payload, mode="rb") as data:
            response = session.put(signed_url, data=data, headers=headers)
    if response.status_code == 200:
        # return json.loads(response._content)
        return True
    else:
        raise RuntimeError(f"Something went wrong: {response.__dict__}")
