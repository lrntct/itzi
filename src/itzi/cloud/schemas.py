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

from datetime import datetime
from pathlib import Path

from itzi_core.data_containers import SimulationConfig
from pydantic import BaseModel, ConfigDict, Field


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


class SimulationTaskSchema(BaseModel):
    """Schema for retrieving simulation task status."""

    team: str
    project: str
    created_on: datetime
    last_updated: datetime
    fingerprint: str
    status: str
    progress: int
    input_bytes: int
    results_bytes: int
    error_stage: str = ""
    error_message: str = ""


class SimulationRequestSchema(BaseModel):
    """Schema for requesting a simulation."""

    project_id: int
    force_rerun: bool = False
    sim_config: SimulationConfig
    dataset_hash: str
    dataset_bytes: int = Field(gt=0, le=1_000_000_000)
    domain_info: DomainInfo


class SimulationResponseSchema(BaseModel):
    """Schema returned when a simulation is created."""

    fingerprint: str
    email: str
    team: str
    project: str
    upload_url: str
    upload_method: str
    upload_headers: dict[str, str]
    upload_expires_at: datetime


class ResultsDownloadResponseSchema(BaseModel):
    """Schema containing instructions for downloading simulation results."""

    fingerprint: str
    download_url: str
    status: str
    download_method: str = "GET"
    download_headers: dict[str, str] = Field(default_factory=dict)
    download_expires_at: datetime | None = None


class TeamSchema(BaseModel):
    id: int
    name: str
    slug: str


class ProjectSchema(BaseModel):
    id: int
    name: str
    slug: str
    team: TeamSchema
