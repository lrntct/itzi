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

from datetime import datetime, timedelta, timezone
import json

import itzi.messenger as msgr

try:
    import requests
    import keyring
except ImportError:
    raise ImportError(
        "To use the cloud functionalities, install itzi with: "
        "'uv tool install itzi[cloud]' "
        "or 'pip install itzi[cloud]'"
    )


def login(url: str, email: str, password: str) -> None:
    """Log into the cloud service.
    Store session token into the system keyring.
    """
    data = {
        "email": str(email),
        "password": str(password),
    }
    with requests.Session() as session:
        response = session.post(url, json=data)
        if response.status_code == 200:
            resp_dict = json.loads(response.text)
        else:
            msgr.fatal(
                f"Authentiction failed. Code: {response.status_code}. Reason: {response.reason}"
            )
    session_token = resp_dict["meta"]["session_token"]
    store_token(email, session_token)
    msgr.message(f"Successfully logged in with account {email}")


def store_token(email: str, token: str, expires_in_days: int = 10) -> None:
    """Store token with expiration timestamp"""
    expiry = datetime.now(timezone.utc) + timedelta(days=expires_in_days)

    token_data = {"token": token, "expires_at": expiry.isoformat()}

    keyring.set_password("itzi_cloud", email, json.dumps(token_data))


def get_valid_token(email: str) -> None | str:
    """Retrieve token. If expired or non-existent, return None"""
    stored = keyring.get_password("itzi_cloud", email)

    if not stored:
        return None

    token_data = json.loads(stored)
    expiry = datetime.fromisoformat(token_data["expires_at"])

    if datetime.now(timezone.utc) >= expiry:
        # Token expired, delete it
        msgr.warning(f"Session for {email} expired, please log again.")
        keyring.delete_password("itzi_cloud", email)
        return None

    return token_data["token"]
