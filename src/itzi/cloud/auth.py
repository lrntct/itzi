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
    keyring.set_password("itzi_cloud", email, session_token)
    msgr.message(f"{email} successfully logged in.")


def get_token(email: str) -> None | str:
    """Retrieve token."""
    return keyring.get_password("itzi_cloud", email)


def logout(url: str, email: str) -> None:
    """Logout from the service. Clear stored token."""
    # Log out
    headers = {"X-Session-Token": get_token(email)}
    with requests.Session() as session:
        response = session.delete(url, headers=headers)
    if response.status_code == 401:
        msgr.message(f"{email} successfuly logged out.")
    # Delete token
    try:
        keyring.delete_password("itzi_cloud", email)
    except keyring.errors.PasswordDeleteError:
        pass  # Already deleted or never existed
