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
from pathlib import Path
from datetime import datetime, timezone
import json
import tempfile
import shutil
from typing import Any

from platformdirs import user_data_dir

from itzi.data_containers import GrassParams


# Metadata schema version
METADATA_VERSION = "1.0"


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

    metadata_file = storage_dir / Path("cloud_simulations.json")

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

    Parameters
    ----------
    metadata_file : Path
        Path to the metadata file.
    """
    initial_data = {"version": METADATA_VERSION, "simulations": {}}

    with open(metadata_file, "w") as f:
        json.dump(initial_data, f, indent=2)


def _load_metadata_file(metadata_file: Path) -> dict[str, Any]:
    """
    Load and parse metadata file.

    Parameters
    ----------
    metadata_file : Path
        Path to the metadata file.

    Returns
    -------
    Dict[str, Any]
        Parsed metadata dictionary.

    Raises
    ------
    ValueError
        If the file is corrupted or has invalid JSON.
    """
    try:
        with open(metadata_file, "r") as f:
            data = json.load(f)

        # Validate basic structure
        if not isinstance(data, dict):
            raise ValueError("Metadata file is corrupted: root is not a dictionary")

        if "version" not in data:
            raise ValueError("Metadata file is corrupted: missing version field")

        if "simulations" not in data:
            raise ValueError("Metadata file is corrupted: missing simulations field")

        return data

    except json.JSONDecodeError as e:
        raise ValueError(f"Metadata file contains invalid JSON: {e}")


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

    Parameters
    ----------
    fingerprint : str
        Unique simulation identifier.
    email : str
        User email address.
    config_file : str
        Path to the configuration file used for this simulation.
    grass_params : GrassParams
        GRASS parameters from the push operation.

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

    # Load existing metadata
    try:
        metadata = _load_metadata_file(metadata_file)
    except (FileNotFoundError, ValueError):
        # If file doesn't exist or is corrupted, start fresh
        _initialize_metadata_file(metadata_file)
        metadata = _load_metadata_file(metadata_file)

    # Create simulation entry
    simulation_data = {
        "email": email,
        "config_file": str(config_file),
        "pushed_at": datetime.now(timezone.utc).isoformat(),
        "grass_params": {
            "grassdata": str(grass_params.grassdata) if grass_params.grassdata else None,
            "location": grass_params.location,
            "mapset": grass_params.mapset,
            "grass_bin": str(grass_params.grass_bin) if grass_params.grass_bin else None,
        },
    }

    # Update metadata
    metadata["simulations"][fingerprint] = simulation_data

    # Write atomically using a temporary file
    # This prevents corruption if the process is interrupted
    temp_fd, temp_path = tempfile.mkstemp(
        dir=metadata_file.parent, prefix=".cloud_simulations_", suffix=".tmp"
    )

    try:
        # Write to temp file
        with open(temp_fd, "w") as f:
            json.dump(metadata, f, indent=2)

        # Atomic move (renames are atomic on POSIX systems)
        shutil.move(temp_path, metadata_file)

    except Exception:
        # Clean up temp file on error
        try:
            Path(temp_path).unlink(missing_ok=True)
        except Exception:
            pass
        raise


def load_simulation_metadata(fingerprint: str) -> GrassParams | None:
    """
    Load GRASS parameters for a simulation from local storage.

    Parameters
    ----------
    fingerprint : str
        Unique simulation identifier.

    Returns
    -------
    Optional[GrassParams]
        GrassParams object if metadata exists, None otherwise.

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
    except (FileNotFoundError, ValueError):
        # File doesn't exist or is corrupted
        return None

    # Check if simulation exists
    if fingerprint not in metadata.get("simulations", {}):
        return None

    sim_data = metadata["simulations"][fingerprint]
    grass_data = sim_data.get("grass_params", {})

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


def list_all_simulations() -> dict[str, dict[str, Any]]:
    """
    List all stored simulation metadata.

    This is a convenience function for debugging and management.

    Returns
    -------
    Dict[str, Dict[str, Any]]
        Dictionary mapping fingerprints to simulation metadata.
        Returns empty dict if no metadata exists or file is corrupted.

    Notes
    -----
    This is a nice-to-have feature for debugging and user convenience.
    """
    metadata_file = get_metadata_file_path()

    if not metadata_file.exists():
        return {}

    try:
        metadata = _load_metadata_file(metadata_file)
        return metadata.get("simulations", {})
    except (FileNotFoundError, ValueError):
        return {}
