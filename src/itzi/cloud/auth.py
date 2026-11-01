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

import getpass
import json
from urllib.parse import urljoin

import itzi.messenger as msgr
from itzi.cloud import urls

try:
    import keyring
    import keyring.errors
    import requests

    from itzi.cloud.http import create_session
except ImportError:
    raise ImportError(
        "To use the cloud functionalities, install itzi with: "
        "'uv tool install itzi[cloud]' "
        "or 'pip install itzi[cloud]'"
    )


def check_login() -> str:
    """Return the email if the user is logged in."""
    email = get_email()
    if not is_logged(email):
        msgr.fatal("Please log in first.")
    return email


def login(email: str, password: str, url: str | None = None) -> None:
    """Log in, prompting for MFA if needed, and store the completed session in the keyring."""
    url = url or urls.get_login_endpoint()
    data = {
        "email": str(email),
        "password": str(password),
    }
    with create_session() as session:
        challenge_token = None
        response = session.post(url, json=data)
        if response.status_code == 401:
            try:
                resp_dict = response.json()
            except requests.exceptions.JSONDecodeError:
                resp_dict = {}
            flows = resp_dict.get("data", {}).get("flows", ())
            if any(
                flow.get("id") == "mfa_authenticate" and flow.get("is_pending") is True
                for flow in flows
            ):
                challenge_token = resp_dict.get("meta", {}).get("session_token")
                if not isinstance(challenge_token, str) or not challenge_token:
                    msgr.fatal("Authentication failed. Missing MFA session token.")
                code = getpass.getpass("MFA code: ").strip()
                response = session.post(
                    urljoin(url, "2fa/authenticate"),
                    json={"code": code},
                    headers={"X-Session-Token": challenge_token},
                )
        if response.status_code != 200:
            msgr.fatal(
                f"Authentication failed. Code: {response.status_code}. Reason: {response.reason}"
            )
        resp_dict = response.json()
        meta = resp_dict["meta"]
        if challenge_token is not None and meta.get("is_authenticated") is not True:
            msgr.fatal("Authentication failed. MFA login did not complete.")
        session_token = meta.get("session_token", challenge_token)
        if not isinstance(session_token, str) or not session_token:
            msgr.fatal("Authentication failed. Missing session token.")
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
    return token


def get_default_email() -> None | str:
    """Retrieve default email from the keyring."""
    return keyring.get_password("itzi_cloud", "default_email")


def logout(email: str, url: str | None = None) -> None:
    """Logout from the service. Clear stored token."""
    url = url or urls.get_session_endpoint()
    token = keyring.get_password("itzi_cloud", email)
    try:
        if token is None:
            msgr.message(f"No stored session found for {email}")
        else:
            try:
                with create_session() as session:
                    response = session.delete(url, headers={"X-Session-Token": token})
            except requests.RequestException as error:
                msgr.warning(f"Could not revoke the remote session: {error}")
            else:
                if response.status_code == 401:
                    msgr.message(f"{email} successfully logged out.")
    finally:
        try:
            keyring.delete_password("itzi_cloud", email)
        except keyring.errors.PasswordDeleteError:
            pass  # Already deleted or never existed


def is_logged(email: str, url: str | None = None) -> bool:
    """Get authentication status."""
    url = url or urls.get_session_endpoint()
    token = keyring.get_password("itzi_cloud", email)
    if token is None:
        return False

    headers = {"X-Session-Token": token}
    with create_session() as session:
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
    default_email = get_default_email()

    if email_cli:
        email = email_cli
    elif default_email:
        email = default_email
    else:
        email = input("Email: ")

    return email
