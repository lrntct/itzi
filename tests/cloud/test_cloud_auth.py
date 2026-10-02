"""Regression tests for optional credentials and logout error handling."""

from collections.abc import Callable
from unittest.mock import MagicMock, Mock

import pytest

from itzi.cloud import auth


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
