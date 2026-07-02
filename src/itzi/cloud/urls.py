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

import os


DEFAULT_API_BASE = "http://localhost:8000"
API_BASE_ENV_VAR = "ITZI_CLOUD_API_BASE"


def get_api_base() -> str:
    return os.environ.get(API_BASE_ENV_VAR, DEFAULT_API_BASE).rstrip("/")


def get_login_endpoint() -> str:
    return f"{get_api_base()}/_allauth/app/v1/auth/login"


def get_session_endpoint() -> str:
    return f"{get_api_base()}/_allauth/app/v1/auth/session"


def get_simulations_endpoint() -> str:
    return f"{get_api_base()}/itzi-api/simulations"


# Backward-compatible module attributes for callers that only need the default values.
API_BASE = get_api_base()
LOGIN_ENDPOINT = get_login_endpoint()
SESSION_ENDPOINT = get_session_endpoint()
SIMULATIONS_ENDPOINT = get_simulations_endpoint()
