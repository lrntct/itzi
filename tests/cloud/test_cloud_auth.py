"""Regression tests for login, MFA, optional credentials, and logout error handling."""

from collections.abc import Callable
from unittest.mock import MagicMock, Mock, call

import pytest

from itzi.cloud import auth
from itzi.messenger import FatalError


@pytest.fixture
def login_mocks(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, Mock, Mock, Mock]:
    session = MagicMock()
    session.__enter__.return_value = session
    session.post.return_value = Mock(status_code=401, reason="Unauthorized")
    session.post.return_value.json.return_value = {
        "data": {"flows": [{"id": "mfa_authenticate", "is_pending": True}]},
        "meta": {"session_token": "incomplete-token", "is_authenticated": False},
    }
    store = Mock()
    prompt = Mock(return_value="012345")
    message = Mock()
    monkeypatch.setenv("ITZI_CLOUD_API_BASE", "https://cloud.example.test/")
    monkeypatch.setattr(auth.requests, "Session", Mock(return_value=session))
    monkeypatch.setattr(auth.keyring, "set_password", store)
    monkeypatch.setattr(auth.getpass, "getpass", prompt)
    monkeypatch.setattr(auth.msgr, "message", message)
    return session, store, prompt, message


def test_login_without_mfa(login_mocks: tuple[MagicMock, Mock, Mock, Mock]) -> None:
    session, store, prompt, message = login_mocks
    session.post.return_value.status_code = 200
    session.post.return_value.json.return_value = {"meta": {"session_token": "token"}}

    auth.login("user@example.com", "secret")

    session.post.assert_called_once_with(
        "https://cloud.example.test/_allauth/app/v1/auth/login",
        json={"email": "user@example.com", "password": "secret"},
    )
    prompt.assert_not_called()
    assert store.call_args_list == [
        call("itzi_cloud", "user@example.com", "token"),
        call("itzi_cloud", "default_email", "user@example.com"),
    ]
    message.assert_called_once_with("user@example.com successfully logged in.")


@pytest.mark.parametrize("new_token", [None, "completed-token"])
@pytest.mark.parametrize("url", [None, "https://other.example.test/prefix/auth/login"])
def test_login_completes_mfa(
    login_mocks: tuple[MagicMock, Mock, Mock, Mock], new_token: str | None, url: str | None
) -> None:
    session, store, prompt, message = login_mocks
    meta: dict[str, str | bool] = {"is_authenticated": True}
    if new_token is not None:
        meta["session_token"] = new_token
    completion = Mock(status_code=200)
    completion.json.return_value = {"meta": meta}
    session.post.side_effect = [session.post.return_value, completion]

    def enter_code(prompt_text: str) -> str:
        store.assert_not_called()
        message.assert_not_called()
        return " 012345 "

    prompt.side_effect = enter_code

    auth.login("user@example.com", "secret", url=url)

    login_url = url or "https://cloud.example.test/_allauth/app/v1/auth/login"
    mfa_url = (
        "https://other.example.test/prefix/auth/2fa/authenticate"
        if url
        else "https://cloud.example.test/_allauth/app/v1/auth/2fa/authenticate"
    )
    assert session.post.call_args_list == [
        call(login_url, json={"email": "user@example.com", "password": "secret"}),
        call(mfa_url, json={"code": "012345"}, headers={"X-Session-Token": "incomplete-token"}),
    ]
    prompt.assert_called_once_with("MFA code: ")
    assert store.call_args_list == [
        call("itzi_cloud", "user@example.com", new_token or "incomplete-token"),
        call("itzi_cloud", "default_email", "user@example.com"),
    ]
    message.assert_called_once_with("user@example.com successfully logged in.")


@pytest.mark.parametrize(
    ("flow_id", "is_pending"),
    [("login", True), ("verify_email", True), ("mfa_authenticate", False)],
)
def test_login_does_not_treat_other_401_responses_as_mfa(
    login_mocks: tuple[MagicMock, Mock, Mock, Mock], flow_id: str, is_pending: bool
) -> None:
    session, store, prompt, message = login_mocks
    session.post.return_value.json.return_value["data"]["flows"] = [
        {"id": flow_id, "is_pending": is_pending}
    ]

    with pytest.raises(FatalError, match="Authentication failed. Code: 401"):
        auth.login("user@example.com", "secret")

    assert session.post.call_count == 1
    prompt.assert_not_called()
    store.assert_not_called()
    message.assert_not_called()


def test_login_handles_non_json_401(login_mocks: tuple[MagicMock, Mock, Mock, Mock]) -> None:
    session, store, prompt, message = login_mocks
    session.post.return_value.json.side_effect = auth.requests.exceptions.JSONDecodeError(
        "Invalid JSON", "Unauthorized", 0
    )

    with pytest.raises(FatalError, match="Authentication failed. Code: 401"):
        auth.login("user@example.com", "secret")

    prompt.assert_not_called()
    store.assert_not_called()
    message.assert_not_called()


def test_login_requires_mfa_session_token(
    login_mocks: tuple[MagicMock, Mock, Mock, Mock],
) -> None:
    session, store, prompt, message = login_mocks
    del session.post.return_value.json.return_value["meta"]["session_token"]

    with pytest.raises(FatalError, match="Missing MFA session token"):
        auth.login("user@example.com", "secret")

    assert session.post.call_count == 1
    prompt.assert_not_called()
    store.assert_not_called()
    message.assert_not_called()


@pytest.mark.parametrize("status_code", [400, 401, 410, 429])
def test_login_does_not_store_token_after_failed_mfa(
    login_mocks: tuple[MagicMock, Mock, Mock, Mock], status_code: int
) -> None:
    session, store, prompt, message = login_mocks
    session.post.side_effect = [
        session.post.return_value,
        Mock(status_code=status_code, reason="MFA failed"),
    ]

    with pytest.raises(FatalError, match=f"Authentication failed. Code: {status_code}"):
        auth.login("user@example.com", "secret")

    prompt.assert_called_once()
    store.assert_not_called()
    message.assert_not_called()


@pytest.mark.parametrize("is_authenticated", [False, None])
def test_login_requires_completed_mfa(
    login_mocks: tuple[MagicMock, Mock, Mock, Mock], is_authenticated: bool | None
) -> None:
    session, store, _prompt, message = login_mocks
    completion = Mock(status_code=200)
    completion.json.return_value = {
        "meta": {"is_authenticated": is_authenticated, "session_token": "still-incomplete"}
    }
    session.post.side_effect = [session.post.return_value, completion]

    with pytest.raises(FatalError, match="MFA login did not complete"):
        auth.login("user@example.com", "secret")

    store.assert_not_called()
    message.assert_not_called()


def test_login_does_not_store_token_after_mfa_connection_error(
    login_mocks: tuple[MagicMock, Mock, Mock, Mock],
) -> None:
    session, store, _prompt, message = login_mocks
    session.post.side_effect = [
        session.post.return_value,
        auth.requests.ConnectionError("Offline"),
    ]

    with pytest.raises(auth.requests.ConnectionError, match="Offline"):
        auth.login("user@example.com", "secret")

    store.assert_not_called()
    message.assert_not_called()


def test_missing_token_is_an_expected_state(monkeypatch: pytest.MonkeyPatch) -> None:
    get_password = Mock(return_value=None)
    delete_password = Mock(side_effect=auth.keyring.errors.PasswordDeleteError("Not found"))
    session = Mock()
    fatal = Mock()
    monkeypatch.setattr(auth.keyring, "get_password", get_password)
    monkeypatch.setattr(auth.keyring, "delete_password", delete_password)
    monkeypatch.setattr(auth.requests, "Session", session)
    monkeypatch.setattr(auth.msgr, "fatal", fatal)

    assert auth.is_logged("user@example.com") is False
    auth.logout("user@example.com")

    delete_password.assert_called_once_with("itzi_cloud", "user@example.com")
    session.assert_not_called()
    fatal.assert_not_called()


@pytest.mark.parametrize("operation", [auth.is_logged, auth.logout])
def test_keyring_errors_propagate(
    monkeypatch: pytest.MonkeyPatch, operation: Callable[[str], bool | None]
) -> None:
    monkeypatch.setattr(
        auth.keyring,
        "get_password",
        Mock(side_effect=auth.keyring.errors.KeyringError("Keyring unavailable")),
    )

    with pytest.raises(auth.keyring.errors.KeyringError, match="Keyring unavailable"):
        operation("user@example.com")


@pytest.mark.parametrize(
    "request_error",
    [None, auth.requests.ConnectionError("Offline"), auth.requests.Timeout("Timed out")],
)
def test_logout_clears_credentials_after_request(
    monkeypatch: pytest.MonkeyPatch, request_error: auth.requests.RequestException | None
) -> None:
    session = MagicMock()
    session.__enter__.return_value = session
    session.delete.side_effect = request_error
    session.delete.return_value.status_code = 401
    delete_password = Mock()
    warning = Mock()
    message = Mock()
    monkeypatch.setattr(auth.keyring, "get_password", Mock(return_value="token"))
    monkeypatch.setattr(auth.keyring, "delete_password", delete_password)
    monkeypatch.setattr(auth.requests, "Session", Mock(return_value=session))
    monkeypatch.setattr(auth.msgr, "warning", warning)
    monkeypatch.setattr(auth.msgr, "message", message)

    auth.logout("user@example.com", url="https://example.test/session")

    session.delete.assert_called_once_with(
        "https://example.test/session", headers={"X-Session-Token": "token"}
    )
    delete_password.assert_called_once_with("itzi_cloud", "user@example.com")
    if request_error is None:
        warning.assert_not_called()
        message.assert_called_once_with("user@example.com successfully logged out.")
    else:
        warning.assert_called_once_with(f"Could not revoke the remote session: {request_error}")
        message.assert_not_called()


def test_logout_cleans_up_but_does_not_hide_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = MagicMock()
    session.__enter__.return_value = session
    session.delete.side_effect = RuntimeError("Unexpected error")
    delete_password = Mock()
    monkeypatch.setattr(auth.keyring, "get_password", Mock(return_value="token"))
    monkeypatch.setattr(auth.keyring, "delete_password", delete_password)
    monkeypatch.setattr(auth.requests, "Session", Mock(return_value=session))

    with pytest.raises(RuntimeError, match="Unexpected error"):
        auth.logout("user@example.com")

    delete_password.assert_called_once_with("itzi_cloud", "user@example.com")
