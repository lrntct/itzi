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

API_BASE = "http://localhost:8000"

LOGIN_ENDPOINT = f"{API_BASE}/_allauth/app/v1/auth/login"
SESSION_ENDPOINT = f"{API_BASE}/_allauth/app/v1/auth/session"
SIMULATIONS_ENDPOINT = f"{API_BASE}/itzi-api/simulations"
