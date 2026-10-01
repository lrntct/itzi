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

from pydantic import BaseModel, Field


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
