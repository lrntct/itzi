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
    set_token(email, session_token)
    msgr.message(f"{email} successfully logged in.")


def set_token(email: str, session_token: str):
    keyring.set_password("itzi_cloud", email, session_token)
    keyring.set_password("itzi_cloud", "default_email", email)


def get_token(email: str) -> None | str:
    """Retrieve token."""
    return keyring.get_password("itzi_cloud", email)


def get_default_email() -> None | str:
    """Retrieve default email from the keyring."""
    return keyring.get_password("itzi_cloud", "default_email")


def logout(url: str, email: str) -> None:
    """Logout from the service. Clear stored token."""
    # Log out
    headers = {"X-Session-Token": get_token(email)}
    with requests.Session() as session:
        response = session.delete(url, headers=headers)
    if response.status_code == 401:
        msgr.message(f"{email} successfully logged out.")
    # Delete token
    try:
        keyring.delete_password("itzi_cloud", email)
    except keyring.errors.PasswordDeleteError:
        pass  # Already deleted or never existed


def is_logged(url: str, email: str) -> None:
    """Get authentication status."""
    headers = {"X-Session-Token": get_token(email)}
    with requests.Session() as session:
        response = session.get(url, headers=headers)
    resp_dict = json.loads(response.text)

    if response.status_code != 200:
        return False

    is_authenticated = resp_dict["meta"]["is_authenticated"]
    if is_authenticated:
        return True
    else:
        raise ValueError(f"Unconsistent response: {resp_dict}")
