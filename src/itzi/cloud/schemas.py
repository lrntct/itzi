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
from typing import Literal

from pydantic import AliasPath, BaseModel, Field


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


class ResultsDownloadResponseSchema(BaseModel):
    """Schema containing instructions for downloading simulation results."""

    fingerprint: str
    download_url: str
    status: str
    download_method: str = "GET"
    download_headers: dict[str, str] = Field(default_factory=dict)
    download_expires_at: datetime | None = None


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


class SimulationResponseSchema(BaseModel):
    simulation_id: str = Field(min_length=1)
    input_id: str
    ensemble_id: str
    member_index: int
    member_label: str


class RunResponseSchema(BaseModel):
    run_id: str = Field(min_length=1)
    simulation_id: str


class RunRequestErrorResponseSchema(BaseModel):
    detail: str
    code: str
    next_action: str
