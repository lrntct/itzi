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
from enum import Enum
from pathlib import Path
import importlib
import os

from itzi.data_containers import GrassParams


class GrassParamsSource(Enum):
    """Indicates where GRASS parameters originated from."""

    ACTIVE_SESSION = "active_session"  # From an active GRASS session
    CONFIG_FILE = "config_file"  # From the configuration file


def is_grass_session_active() -> bool:
    """
    Check if a GRASS session is currently active.

    A GRASS session is considered active when:
    1. The grass.script module can be imported
    2. Environment variables are set: GISDBASE, LOCATION_NAME, MAPSET
    3. The specified paths exist and are accessible

    Returns
    -------
    bool
        True if a GRASS session is active and valid, False otherwise.
    """
    # Check if grass.script can be imported
    if not importlib.util.find_spec("grass"):
        return False

    # Check if required environment variables are set
    gisdbase = os.environ.get("GISDBASE")
    location_name = os.environ.get("LOCATION_NAME")
    mapset = os.environ.get("MAPSET")

    if not all([gisdbase, location_name, mapset]):
        return False

    # Validate that paths exist and are accessible
    gisdbase_path = Path(gisdbase)
    location_path = gisdbase_path / location_name
    mapset_path = location_path / mapset

    if not gisdbase_path.exists() or not gisdbase_path.is_dir():
        return False

    if not location_path.exists() or not location_path.is_dir():
        return False

    if not mapset_path.exists() or not mapset_path.is_dir():
        return False

    # Check that we have appropriate permissions
    if not os.access(gisdbase_path, os.R_OK):
        return False

    if not os.access(location_path, os.R_OK):
        return False

    if not os.access(mapset_path, os.W_OK):
        return False

    return True


def get_active_grass_params() -> GrassParams | None:
    """
    Extract GRASS parameters from the currently active GRASS session.

    This function retrieves the grassdata, location, and mapset from
    environment variables set by an active GRASS session.

    Returns
    -------
    GrassParams | None
        GrassParams object with session information if a session is active,
        None otherwise.

    Notes
    -----
    - Region and mask are not included as they're input processing parameters
    - grass_bin is not included as it's not available from environment
    """
    if not is_grass_session_active():
        return None

    return GrassParams(
        grassdata=os.environ.get("GISDBASE"),
        location=os.environ.get("LOCATION_NAME"),
        mapset=os.environ.get("MAPSET"),
        region=None,
        mask=None,
        grass_bin=None,
    )


def get_grass_params_from_env(
    config_grass_params: GrassParams,
) -> tuple[GrassParams, GrassParamsSource]:
    """
    Determine GRASS parameters with priority logic.

    Priority order:
    1. Active GRASS session (highest priority)
       - Uses session's grassdata/location/mapset
       - Merges region/mask from config file for input processing
    2. Configuration file - Falls back to GRASS params in config

    Parameters
    ----------
    config_grass_params : GrassParams
        GRASS parameters from the configuration file, which may include
        region and mask for input processing.

    Notes
    -----
    - If an active session exists, its grassdata/location/mapset take priority
    - Region and mask from config are always used when provided (for input processing)
    - grass_bin from config is preserved as fallback

    """

    session_params = get_active_grass_params()

    if session_params is not None:
        # Active session exists - merge with config for region/mask
        merged_params = GrassParams(
            grassdata=session_params.grassdata,
            location=session_params.location,
            mapset=session_params.mapset,
            region=config_grass_params.region,  # Use config's region for input processing
            mask=config_grass_params.mask,  # Use config's mask for input processing
            grass_bin=config_grass_params.grass_bin,  # Use config's grass_bin as fallback
        )
        return merged_params, GrassParamsSource.ACTIVE_SESSION

    # No active session - use config file parameters
    return config_grass_params, GrassParamsSource.CONFIG_FILE
