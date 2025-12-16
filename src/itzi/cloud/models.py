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

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from itzi.data_containers import SimulationConfig


class DomainInfo(BaseModel):
    """Domain information for the simulation grid."""

    model_config = ConfigDict(frozen=True)

    rows: int
    cols: int
    ewres: float
    nsres: float


class InputInfo(BaseModel):
    """Store information about input data."""

    model_config = ConfigDict(frozen=True)

    sim_config: SimulationConfig
    dataset_path: Path  # Path to the tgz file containing the zarr of input maps
    dataset_hash: str  # Base64 MD5 of the dataset
    dataset_bytes: int
    domain_info: DomainInfo
