"""Shared HTTP session defaults for Itzi cloud requests."""

import requests


def create_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = "itzi"
    return session
