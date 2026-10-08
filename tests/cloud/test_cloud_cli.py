"""Test cloud CLI helpers and entrypoints."""

import argparse
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from itzi.cloud.cli import (
    itzi_cloud_login,
    itzi_cloud_project,
    itzi_cloud_pull,
    itzi_cloud_push,
    itzi_cloud_status,
)
from itzi.messenger import VerbosityLevel


def install_stub_module(monkeypatch, module_name: str, **attrs) -> ModuleType:
    module = ModuleType(module_name)
    for name, value in attrs.items():
        setattr(module, name, value)
    monkeypatch.setitem(sys.modules, module_name, module)
    return module


def test_itzi_cloud_login_reports_authenticated_status(monkeypatch):
    messages = []
    install_stub_module(
        monkeypatch,
        "itzi.cloud.auth",
        get_email=lambda email: email,
        is_logged=lambda email: True,
        login=lambda **kwargs: pytest.fail("login should not be called"),
        logout=lambda **kwargs: pytest.fail("logout should not be called"),
    )
    monkeypatch.setattr("itzi.cloud.cli.msgr.message", messages.append)

    itzi_cloud_login(
        argparse.Namespace(
            email="user@example.com",
            password=None,
            logout=False,
            status=True,
        )
    )

    assert messages == ["user@example.com IS authenticated."]


def test_itzi_cloud_login_logs_out(monkeypatch):
    calls = []
    install_stub_module(
        monkeypatch,
        "itzi.cloud.auth",
        get_email=lambda email: email,
        is_logged=lambda email: pytest.fail("status should not be checked"),
        login=lambda **kwargs: pytest.fail("login should not be called"),
        logout=lambda email: calls.append(email),
    )

    itzi_cloud_login(
        argparse.Namespace(
            email="user@example.com",
            password=None,
            logout=True,
            status=False,
        )
    )

    assert calls == ["user@example.com"]


def test_itzi_cloud_login_uses_password_argument(monkeypatch):
    calls = []
    install_stub_module(
        monkeypatch,
        "itzi.cloud.auth",
        get_email=lambda email: email,
        is_logged=lambda email: pytest.fail("status should not be checked"),
        login=lambda email, password: calls.append((email, password)),
        logout=lambda **kwargs: pytest.fail("logout should not be called"),
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt: pytest.fail("prompt should not run"))

    itzi_cloud_login(
        argparse.Namespace(
            email="user@example.com",
            password="secret",
            logout=False,
            status=False,
        )
    )

    assert calls == [("user@example.com", "secret")]


def test_itzi_cloud_login_prompts_for_password(monkeypatch):
    calls = []
    prompts = []
    install_stub_module(
        monkeypatch,
        "itzi.cloud.auth",
        get_email=lambda email: email,
        is_logged=lambda email: pytest.fail("status should not be checked"),
        login=lambda email, password: calls.append((email, password)),
        logout=lambda **kwargs: pytest.fail("logout should not be called"),
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt: prompts.append(prompt) or "secret")

    itzi_cloud_login(
        argparse.Namespace(
            email="user@example.com",
            password=None,
            logout=False,
            status=False,
        )
    )

    assert prompts == ["user@example.com's password: "]
    assert calls == [("user@example.com", "secret")]


def test_itzi_cloud_push_creates_ensemble_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    calls = []
    messages = []
    install_stub_module(
        monkeypatch,
        "itzi.cloud.auth",
        check_login=lambda: "user@example.com",
        get_token=lambda email: "token",
    )
    install_stub_module(
        monkeypatch,
        "itzi.cloud.archive",
        build_archives=lambda path, token: (
            calls.append((path, token))
            or (
                SimpleNamespace(
                    ensemble=SimpleNamespace(
                        source=SimpleNamespace(path=Path(path), document_index=0)
                    ),
                    path=Path("/tmp/input.tzst"),
                    size_bytes=42,
                    sha256="a" * 64,
                ),
            )
        ),
    )
    install_stub_module(
        monkeypatch,
        "itzi.cloud.push",
        create_ensemble=lambda archive, **kwargs: (
            calls.append((archive.path, kwargs))
            or SimpleNamespace(ensemble_id="ensemble-1", input_id="input-1", run_ids={})
        ),
        upload_input=lambda archive, draft, token: (
            calls.append((archive.path, draft.input_id, token)) or draft
        ),
        wait_for_input=lambda draft, token: draft,
        launch_runs=lambda archive, draft, token: SimpleNamespace(
            ensemble_id=draft.ensemble_id, input_id=draft.input_id, run_ids={0: "run-1"}
        ),
    )
    monkeypatch.setattr("itzi.cloud.cli.msgr.message", messages.append)
    itzi_cloud_push(argparse.Namespace(project="project-id", config_file=["one.yml"]))
    assert calls == [
        ("one.yml", "token"),
        (
            Path("/tmp/input.tzst"),
            {
                "project_id": "project-id",
                "email": "user@example.com",
                "session_token": "token",
                "force": False,
            },
        ),
        (Path("/tmp/input.tzst"), "input-1", "token"),
    ]
    assert "Ensemble ensemble-1, Input input-1 accepted; runs: 0=run-1" in messages[0]


@pytest.mark.parametrize("ensemble_id", [None, "ensemble-123"])
def test_itzi_cloud_status_displays_submissions(
    monkeypatch: pytest.MonkeyPatch, ensemble_id: str | None
) -> None:
    calls = {"get_statuses": [], "display": [], "table": []}
    monkeypatch.delenv("ITZI_VERBOSE", raising=False)
    install_stub_module(
        monkeypatch,
        "itzi.cloud.auth",
        check_login=lambda: "user@example.com",
        get_token=lambda email: "token-123",
    )
    install_stub_module(
        monkeypatch,
        "itzi.cloud.status",
        get_ensemble_statuses=lambda session_token, email, ensemble_id: (
            calls["get_statuses"].append((session_token, email, ensemble_id))
            or ("ensemble status",)
        ),
        display_ensemble_statuses=lambda statuses: calls["display"].append(statuses),
        display_ensemble_table=lambda statuses: calls["table"].append(statuses),
    )

    itzi_cloud_status(argparse.Namespace(ensemble_id=ensemble_id))

    assert os.environ["ITZI_VERBOSE"] == str(VerbosityLevel.MESSAGE)
    assert calls["get_statuses"] == [("token-123", "user@example.com", ensemble_id)]
    assert calls["display"] == ([("ensemble status",)] if ensemble_id is not None else [])
    assert calls["table"] == ([("ensemble status",)] if ensemble_id is None else [])


def test_itzi_cloud_status_reports_lookup_failure(monkeypatch) -> None:
    from itzi.messenger import FatalError

    install_stub_module(
        monkeypatch,
        "itzi.cloud.auth",
        check_login=lambda: "user@example.com",
        get_token=lambda email: "token-123",
    )
    install_stub_module(
        monkeypatch,
        "itzi.cloud.status",
        get_ensemble_statuses=lambda **kwargs: (_ for _ in ()).throw(
            ValueError("No local submission found for Ensemble missing")
        ),
        display_ensemble_statuses=lambda statuses: pytest.fail("display should not run"),
        display_ensemble_table=lambda statuses: pytest.fail("display should not run"),
    )

    with pytest.raises(FatalError, match="Cloud status failed: No local submission"):
        itzi_cloud_status(argparse.Namespace(ensemble_id="missing"))


def test_itzi_cloud_project_displays_project_list(monkeypatch):
    calls = {"get_projects_list": [], "display": []}
    monkeypatch.delenv("ITZI_VERBOSE", raising=False)
    install_stub_module(
        monkeypatch,
        "itzi.cloud.auth",
        check_login=lambda: "user@example.com",
        get_token=lambda email: "token-123",
    )
    install_stub_module(
        monkeypatch,
        "itzi.cloud.project",
        get_projects_list=lambda session_token: (
            calls["get_projects_list"].append(session_token) or [{"project_id": "proj-abc123"}]
        ),
        display_projects_list=lambda projects: calls["display"].append(projects),
    )

    itzi_cloud_project(argparse.Namespace(list=True))

    assert os.environ["ITZI_VERBOSE"] == str(VerbosityLevel.MESSAGE)
    assert calls["get_projects_list"] == ["token-123"]
    assert calls["display"] == [[{"project_id": "proj-abc123"}]]


def test_itzi_cloud_pull_downloads_verified_package(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    messages = []
    monkeypatch.delenv("ITZI_VERBOSE", raising=False)
    install_stub_module(
        monkeypatch,
        "itzi.cloud.auth",
        check_login=lambda: "user@example.com",
        get_token=lambda email: "token-123",
    )
    install_stub_module(
        monkeypatch,
        "itzi.cloud.pull",
        pull_result_package=lambda **kwargs: calls.append(kwargs) or kwargs["destination"],
    )
    monkeypatch.setattr("itzi.cloud.cli.msgr.message", messages.append)

    itzi_cloud_pull(
        argparse.Namespace(
            ensemble_id="ensemble-123",
            run_id=["run-2", "run-1"],
            output="results.archive",
        )
    )

    assert os.environ["ITZI_VERBOSE"] == str(VerbosityLevel.MESSAGE)
    assert messages == ["Verified result package saved to results.archive"]
    assert calls == [
        {
            "session_token": "token-123",
            "email": "user@example.com",
            "ensemble_id": "ensemble-123",
            "run_ids": ("run-2", "run-1"),
            "destination": Path("results.archive"),
        }
    ]
