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

from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

from itzi_core.const import InfiltrationModelType, TemporalType
from itzi_core.data_containers import SimulationConfig
from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    project_slug: str
    created_on: datetime
    last_updated: datetime
    fingerprint: str
    status: str
    progress: int
    input_bytes: int
    results_bytes: int
    error_stage: str = ""
    error_message: str = ""


class SurfaceFlowParametersSchema(BaseModel):
    """Surface-flow parameters accepted by the cloud API."""

    hmin: float
    cfl: float
    theta: float
    g: float
    vrouting: float
    dtmax: float
    slope_threshold: float
    max_slope: float
    max_error: float


class SimulationConfigSchema(BaseModel):
    """Stable wire representation of a simulation configuration."""

    start_time: datetime
    end_time: datetime
    record_step: timedelta
    temporal_type: TemporalType
    input_map_names: dict[str, str | None]
    output_map_names: dict[str, str | None]
    surface_flow_parameters: SurfaceFlowParametersSchema
    stats_file: str
    dtinf: float
    infiltration_model: InfiltrationModelType
    swmm_inp: str | None
    drainage_output: str | None
    orifice_coeff: float
    free_weir_coeff: float
    submerged_weir_coeff: float

    @model_validator(mode="before")
    @classmethod
    def normalize_core_config(cls, value: object) -> object:
        """Adapt the current itzi-core model to the cloud API contract."""
        if not isinstance(value, SimulationConfig):
            return value

        normalized = value.model_dump(mode="python")
        normalized.pop("hotstart_config", None)
        normalized["stats_file"] = str(normalized.get("stats_file") or "")
        if normalized.get("swmm_inp") is not None:
            normalized["swmm_inp"] = str(normalized["swmm_inp"])

        surface_parameters = normalized["surface_flow_parameters"]
        if not isinstance(surface_parameters, Mapping):
            surface_parameters = surface_parameters.model_dump(mode="python")
        normalized["surface_flow_parameters"] = {
            **surface_parameters,
            # Rain routing was removed from itzi-core, but this API still requires its old default.
            "vrouting": 0.1,
        }
        return normalized


class SimulationRequestSchema(BaseModel):
    """Schema for requesting a simulation."""

    project_slug: str = Field(min_length=1, max_length=100, pattern=r"^[-a-zA-Z0-9_]+$")
    force_rerun: bool = False
    sim_config: SimulationConfigSchema
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
    name: str
    slug: str


class ProjectSchema(BaseModel):
    name: str
    slug: str
    team: TeamSchema
