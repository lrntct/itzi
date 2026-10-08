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

from datetime import datetime
from typing import Annotated, Literal

from pydantic import AliasPath, AwareDatetime, BaseModel, Field


class PackageRequestSchema(BaseModel):
    run_ids: tuple[Annotated[str, Field(min_length=1)], ...] = Field(min_length=1, max_length=100)


class PackageRequestResponseSchema(BaseModel):
    package_id: str = Field(min_length=1)
    state: Literal["queued"] = "queued"
    manifest_sha256: str


class PackageDownloadSchema(BaseModel):
    method: Literal["GET"]
    url: str
    headers: dict[str, str]
    url_expires_at: AwareDatetime
    artifact_expires_at: AwareDatetime
    size_bytes: int = Field(ge=0)
    archive_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class PackageStatusResponseSchema(BaseModel):
    package_id: str
    state: Literal["queued", "running", "ready", "failed"]
    manifest_sha256: str
    failure_code: Literal["", "package_export_failed"] = ""
    download: PackageDownloadSchema | None = None


class TeamSchema(BaseModel):
    team_id: str
    name: str


class ProjectSchema(BaseModel):
    project_id: str
    name: str
    team: TeamSchema


class EnsembleInputResponseSchema(BaseModel):
    ensemble_id: str = Field(min_length=1)
    input_id: str = Field(min_length=1)


class InputUploadInstructionSchema(BaseModel):
    input_id: str
    transfer_id: str = Field(min_length=1)
    method: Literal["PUT"]
    url: str
    headers: dict[str, str]
    expires_at: datetime
    size_bytes: int
    content_type: Literal["application/zstd"]


class InputConfirmationSchema(BaseModel):
    input_id: str
    state: str


class UploadConfirmationSchema(BaseModel):
    size_bytes: int
    sha256: str


class InputUploadStatusSchema(BaseModel):
    input_id: str
    state: str
    upload_confirmation: UploadConfirmationSchema | None
    failure: InputFailureSchema | None = None
    members: list[AcceptedMemberSchema] | None = Field(
        default=None, validation_alias=AliasPath("acceptance", "member_mapping", "members")
    )


class InputFailureSchema(BaseModel):
    message: str
    next_action: str


class AcceptedMemberSchema(BaseModel):
    index: int
    label: str


class InputStorageSchema(BaseModel):
    reserved_bytes: int
    accepted_bytes: int


class AccountStorageSchema(InputStorageSchema):
    entitlement_bytes: int
    available_bytes: int


class AcceptedVariableSchema(BaseModel):
    name: str
    dtype: str
    dimensions: tuple[str, ...]
    shape: tuple[int, ...]
    member_scope: str
    time_coordinate: str | None = None


class CellEdgeExtentSchema(BaseModel):
    minimum_x: float
    minimum_y: float
    maximum_x: float
    maximum_y: float


class CoordinateSummarySchema(BaseModel):
    name: str
    axis: str
    dtype: str
    size: int
    first: int | float | str
    last: int | float | str
    resolution: float | None = None
    unit: str | None = None


class TimeCoverageSchema(BaseModel):
    coordinate_name: str
    start: int | float | str
    end: int | float | str
    unit: str | None = None


class SupportedMetadataSchema(BaseModel):
    zarr_format: int = 3
    temporal_type: str


class InputAcceptanceSchema(BaseModel):
    """Public scientific facts, without storage locators or arbitrary metadata."""

    contract_revision: str
    accepted_at: datetime
    variables: tuple[AcceptedVariableSchema, ...] = Field(
        validation_alias=AliasPath("variable_inventory", "variables")
    )
    crs_wkt: str = Field(validation_alias=AliasPath("crs", "wkt"))
    extent: CellEdgeExtentSchema
    coordinate_summaries: tuple[CoordinateSummarySchema, ...]
    time_coverage: tuple[TimeCoverageSchema, ...]
    supported_metadata: SupportedMetadataSchema


class InputDetailFailureSchema(InputFailureSchema):
    kind: str
    code: str


class InputDetailSchema(InputUploadStatusSchema):
    project_id: str
    ensemble_id: str
    state_changed_at: datetime
    failure: InputDetailFailureSchema | None = None
    acceptance: InputAcceptanceSchema | None
    input_storage: InputStorageSchema
    account_storage: AccountStorageSchema


class SimulationResponseSchema(BaseModel):
    simulation_id: str = Field(min_length=1)
    input_id: str
    ensemble_id: str
    member_index: int
    member_label: str


class RunResponseSchema(BaseModel):
    run_id: str = Field(min_length=1)
    simulation_id: str


class RequestErrorResponseSchema(BaseModel):
    detail: str
    code: str
    next_action: str
