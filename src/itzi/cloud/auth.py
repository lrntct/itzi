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
import json

import itzi.messenger as msgr
from itzi.cloud import urls

try:
    import requests
    import keyring
    import keyring.errors
except ImportError:
    raise ImportError(
        "To use the cloud functionalities, install itzi with: "
        "'uv tool install itzi[cloud]' "
        "or 'pip install itzi[cloud]'"
    )


def login(email: str, password: str, url: str = urls.LOGIN_ENDPOINT) -> None:
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
                f"Authentication failed. Code: {response.status_code}. Reason: {response.reason}"
            )
    session_token = resp_dict["meta"]["session_token"]
    set_token(email, session_token)
    msgr.message(f"{email} successfully logged in.")


def set_token(email: str, session_token: str) -> None:
    keyring.set_password("itzi_cloud", email, session_token)
    keyring.set_password("itzi_cloud", "default_email", email)


def get_token(email: str) -> str:
    """Retrieve token."""
    token = keyring.get_password("itzi_cloud", email)
    if token is None:
        msgr.fatal(
            f"No authentication token found for {email}. "
            "Please log in first using 'itzi cloud login'."
        )
        assert False, "Here for type narrowing, remove when ty can understand it"
    return token


def get_default_email() -> None | str:
    """Retrieve default email from the keyring."""
    return keyring.get_password("itzi_cloud", "default_email")


def logout(email: str, url: str = urls.SESSION_ENDPOINT) -> None:
    """Logout from the service. Clear stored token."""
    # 1. Log out
    try:
        token = get_token(email)
        # Log out from server
        headers = {"X-Session-Token": token}
        with requests.Session() as session:
            response = session.delete(url, headers=headers)
        if response.status_code == 401:
            msgr.message(f"{email} successfully logged out.")
    except Exception:
        # No token found, but still try to delete local credentials
        msgr.message(f"No active session found for {email}")

    # 2. Delete token
    try:
        keyring.delete_password("itzi_cloud", email)
    except keyring.errors.PasswordDeleteError:
        pass  # Already deleted or never existed


def is_logged(email: str, url: str = urls.SESSION_ENDPOINT) -> bool:
    """Get authentication status."""
    try:
        token = get_token(email)
    except Exception:
        # No token found = not logged in
        return False

    headers = {"X-Session-Token": token}
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


def get_email(email_cli: str | None = None) -> str:
    """ """
    default_email = get_default_email()

    if email_cli:
        email = email_cli
    elif default_email:
        email = default_email
    else:
        email = input("Email: ")

    return email
