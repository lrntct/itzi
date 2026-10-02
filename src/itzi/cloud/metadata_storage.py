"""
Copyright (C) 2025-2026 Laurent Courty

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

import json
import tempfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal, TypedDict

from platformdirs import user_data_dir
from pydantic import BaseModel, ConfigDict, Field, ValidationError, with_config

from itzi.grass.session import GrassParams

# Metadata schema version
METADATA_VERSION = "1.0"


class EnsembleDraft(BaseModel):
    """Locally durable identity and server IDs for one YAML document and Input."""

    model_config = ConfigDict(frozen=True, extra="allow")

    email: str
    project_id: str
    member_labels: tuple[str, ...]
    grass_params: GrassParams
    archive_sha256: str
    idempotency_key: str
    yaml_sha256: str
    ensemble_id: str | None = None
    input_id: str | None = None
    upload_stage: Literal["uploaded", "confirmed"] | None = None
    transfer_id: str | None = None
    confirmation_state: str | None = None
    simulation_ids: dict[int, str] = Field(default_factory=dict)
    run_ids: dict[int, str] = Field(default_factory=dict)


@with_config(ConfigDict(extra="allow"))
class StoredGrassParams(TypedDict):
    grassdata: str | None
    location: str | None
    mapset: str | None
    grass_bin: str | None


@with_config(ConfigDict(extra="allow"))
class SimulationRecord(TypedDict):
    email: str
    config_file: str
    pushed_at: str
    grass_params: StoredGrassParams


class MetadataFile(BaseModel):
    model_config = ConfigDict(extra="allow")

    version: str
    simulations: dict[str, SimulationRecord]
    ensembles: dict[str, EnsembleDraft] = Field(default_factory=dict)


def _write_metadata_file(metadata_file: Path, metadata: MetadataFile) -> None:
    temp_fd, temp_path = tempfile.mkstemp(
        dir=metadata_file.parent, prefix=".cloud_", suffix=".tmp"
    )
    try:
        with open(temp_fd, "w") as file:
            json.dump(metadata.model_dump(mode="json"), file, indent=2)
        Path(temp_path).replace(metadata_file)
    finally:
        Path(temp_path).unlink(missing_ok=True)


def save_ensemble_draft(draft: EnsembleDraft) -> None:
    """Persist a create attempt (or its response) before advancing to the next step."""
    metadata_file = get_metadata_file_path()
    metadata = _load_metadata_file(metadata_file)
    metadata.ensembles[draft.idempotency_key] = draft
    _write_metadata_file(metadata_file, metadata)


def get_or_create_ensemble_draft(
    email: str,
    project_id: str,
    member_labels: tuple[str, ...],
    grass_params: GrassParams,
    archive_sha256: str,
    yaml_sha256: str,
    *,
    force: bool = False,
) -> EnsembleDraft:
    """Resume the same Input, or create a new Ensemble for a changed document/Input."""
    key = sha256(json.dumps((email, project_id, yaml_sha256, archive_sha256)).encode()).hexdigest()
    metadata = _load_metadata_file(get_metadata_file_path())
    stored = metadata.ensembles.get(key)
    if stored is not None:
        return stored
    if not force and any(
        draft.email == email
        and draft.project_id == project_id
        and draft.yaml_sha256 == yaml_sha256
        for draft in metadata.ensembles.values()
    ):
        raise ValueError(
            "The input archive differs from the one recorded for this yaml document; "
            "use --force to create a new Ensemble."
        )
    draft = EnsembleDraft(
        email=email,
        project_id=project_id,
        member_labels=member_labels,
        grass_params=grass_params,
        archive_sha256=archive_sha256,
        idempotency_key=key,
        yaml_sha256=yaml_sha256,
    )
    save_ensemble_draft(draft)
    return draft


def get_metadata_file_path() -> Path:
    """
    Get the path to the metadata file with proper permissions.

    Creates the storage directory if it doesn't exist and sets restrictive
    permissions on both the directory (0700) and the metadata file (0600)
    to protect filesystem paths.

    Returns
    -------
    Path
        Path to the metadata file.
    """
    storage_dir = Path(user_data_dir("itzi", "ItziModel"))

    # Create directory with owner-only permissions (0700)
    storage_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Ensure directory has correct permissions even if it already existed
    storage_dir.chmod(0o700)

    metadata_file = storage_dir / Path("cloud_ensembles.json")

    # Set restrictive permissions on metadata file (0600 - owner read/write only)
    if not metadata_file.exists():
        # Create empty file with restrictive permissions
        metadata_file.touch(mode=0o600)
        # Initialize with empty metadata structure
        _initialize_metadata_file(metadata_file)
    else:
        # Ensure existing file has correct permissions
        metadata_file.chmod(0o600)

    return metadata_file


def _initialize_metadata_file(metadata_file: Path) -> None:
    """
    Initialize a new metadata file with the base structure.
    """
    initial_data = {"version": METADATA_VERSION, "simulations": {}}

    with open(metadata_file, "w") as f:
        json.dump(initial_data, f, indent=2)


def _load_metadata_file(metadata_file: Path) -> MetadataFile:
    """
    Load and parse metadata file.

    Parameters
    ----------
    metadata_file : Path
        Path to the metadata file.

    Raises
    ------
    ValueError
        If the file is corrupted or has invalid JSON.
    """
    try:
        with open(metadata_file, "r") as f:
            data = json.load(f)

        return MetadataFile.model_validate(data)

    except json.JSONDecodeError as e:
        raise ValueError(f"Metadata file contains invalid JSON: {e}") from e
    except ValidationError as e:
        raise ValueError(f"Metadata file is corrupted: {e}") from e


def save_simulation_metadata(
    fingerprint: str,
    email: str,
    config_file: str,
    grass_params: GrassParams,
) -> None:
    """
    Save simulation metadata to local storage with atomic writes.

    This function stores the GRASS session information for a pushed simulation
    so it can be retrieved later during pull operations.


    Notes
    -----
    Uses atomic writes (write to temp file + rename) to prevent corruption.
    Only stores grassdata, location, mapset, and grass_bin from GrassParams.
    Region and mask are not stored as they're only used for input processing.

    Raises
    ------
    OSError
        If file operations fail.
    ValueError
        If the existing metadata file is corrupted.
    """
    metadata_file = get_metadata_file_path()

    metadata = _load_metadata_file(metadata_file)

    simulation_data: SimulationRecord = {
        "email": email,
        "config_file": str(config_file),
        "pushed_at": datetime.now(UTC).isoformat(),
        "grass_params": {
            "grassdata": str(grass_params.grassdata) if grass_params.grassdata else None,
            "location": grass_params.location,
            "mapset": grass_params.mapset,
            "grass_bin": str(grass_params.grass_bin) if grass_params.grass_bin else None,
        },
    }

    metadata.simulations[fingerprint] = simulation_data

    _write_metadata_file(metadata_file, metadata)


def load_simulation_metadata(fingerprint: str) -> GrassParams | None:
    """
    Load GRASS parameters for a simulation from local storage.

    Notes
    -----
    Validates that paths exist before returning GrassParams.
    Returns None if metadata doesn't exist or paths are invalid.
    """
    metadata_file = get_metadata_file_path()

    # Check if metadata file exists
    if not metadata_file.exists():
        return None

    try:
        metadata = _load_metadata_file(metadata_file)
    except FileNotFoundError, ValueError:
        # File doesn't exist or is corrupted
        return None

    # Check if simulation exists
    if fingerprint not in metadata.simulations:
        return None

    sim_data = metadata.simulations[fingerprint]
    grass_data = sim_data["grass_params"]

    # Extract parameters
    grassdata = grass_data.get("grassdata")
    location = grass_data.get("location")
    mapset = grass_data.get("mapset")
    grass_bin = grass_data.get("grass_bin")

    # Validate required fields
    if not grassdata or not location or not mapset:
        return None

    # Validate that grassdata path exists
    grassdata_path = Path(grassdata)
    if not grassdata_path.exists():
        return None

    # Validate that location exists
    location_path = grassdata_path / location
    if not location_path.exists():
        return None

    # Validate that mapset exists
    mapset_path = location_path / mapset
    if not mapset_path.exists():
        return None

    # Create GrassParams object
    # Note: region and mask are not stored/loaded as they're only for input processing
    return GrassParams(
        grassdata=str(grassdata_path),
        location=location,
        mapset=mapset,
        region=None,
        mask=None,
        grass_bin=grass_bin,  # Keep as string/None, GrassParams will handle conversion
    )


def list_all_simulations() -> dict[str, SimulationRecord]:
    """
    List all stored simulation metadata.

    This is a convenience function for debugging and management.

    Returns an empty dict if no metadata exists or the file is corrupted.

    Notes
    -----
    This is a nice-to-have feature for debugging and user convenience.
    """
    metadata_file = get_metadata_file_path()

    if not metadata_file.exists():
        return {}

    try:
        metadata = _load_metadata_file(metadata_file)
        return metadata.simulations
    except FileNotFoundError, ValueError:
        return {}
