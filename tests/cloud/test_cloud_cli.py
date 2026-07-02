"""Test cloud CLI helpers and entrypoints."""

import argparse
import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

from itzi.cloud.cli import (
    itzi_cloud_login,
    itzi_cloud_pull,
    itzi_cloud_push,
    itzi_cloud_status,
    resolve_cloud_pull_grass_params,
)
from itzi.const import VerbosityLevel
from itzi.data_containers import GrassParams
from itzi.itzi_error import ItziFatal


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


def test_itzi_cloud_push_submits_and_saves_metadata(monkeypatch):
    calls = {
        "create_request": [],
        "request_simulation": [],
        "upload_input": [],
        "confirm_upload": [],
        "save_simulation_metadata": [],
    }
    request_data = SimpleNamespace(dataset_hash="hash-123")
    grass_params = GrassParams(grassdata="/db", location="loc", mapset="mapset")
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
        "itzi.cloud.push",
        create_request=lambda project, conf_file, force: (
            calls["create_request"].append((project, conf_file, force))
            or (request_data, "/tmp/input.tgz", grass_params)
        ),
        request_simulation=lambda session_token, metadata: (
            calls["request_simulation"].append((session_token, metadata))
            or {"upload_url": "https://example.test/upload", "fingerprint": "fp-123"}
        ),
        upload_input=lambda signed_url, payload, content_md5, content_type: (
            calls["upload_input"].append((signed_url, payload, content_md5, content_type)) or True
        ),
        confirm_upload=lambda session_token, fingerprint: calls["confirm_upload"].append(
            (session_token, fingerprint)
        ),
    )
    install_stub_module(
        monkeypatch,
        "itzi.cloud.metadata_storage",
        save_simulation_metadata=lambda **kwargs: calls["save_simulation_metadata"].append(kwargs),
    )
    monkeypatch.setattr("itzi.cloud.cli.msgr.message", messages.append)

    itzi_cloud_push(argparse.Namespace(project=42, force=True, config_file=["sim.ini"]))

    assert os.environ["ITZI_VERBOSE"] == str(VerbosityLevel.MESSAGE)
    assert calls["create_request"] == [(42, "sim.ini", True)]
    assert calls["request_simulation"] == [("token-123", request_data)]
    assert calls["upload_input"] == [
        ("https://example.test/upload", "/tmp/input.tgz", "hash-123", "application/gzip")
    ]
    assert calls["confirm_upload"] == [("token-123", "fp-123")]
    assert calls["save_simulation_metadata"] == [
        {
            "fingerprint": "fp-123",
            "email": "user@example.com",
            "config_file": "sim.ini",
            "grass_params": grass_params,
        }
    ]
    assert messages == [
        "sim.ini: Uploading input data...",
        "sim.ini: Uploading input data success!",
    ]


def test_itzi_cloud_push_warns_when_metadata_save_fails(monkeypatch):
    warnings = []
    request_data = SimpleNamespace(dataset_hash="hash-123")
    grass_params = GrassParams(grassdata="/db", location="loc", mapset="mapset")
    install_stub_module(
        monkeypatch,
        "itzi.cloud.auth",
        check_login=lambda: "user@example.com",
        get_token=lambda email: "token-123",
    )
    install_stub_module(
        monkeypatch,
        "itzi.cloud.push",
        create_request=lambda project, conf_file, force: (
            request_data,
            "/tmp/input.tgz",
            grass_params,
        ),
        request_simulation=lambda session_token, metadata: {
            "upload_url": "https://example.test/upload",
            "fingerprint": "fp-123",
        },
        upload_input=lambda **kwargs: True,
        confirm_upload=lambda session_token, fingerprint: None,
    )
    install_stub_module(
        monkeypatch,
        "itzi.cloud.metadata_storage",
        save_simulation_metadata=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr("itzi.cloud.cli.msgr.warning", warnings.append)

    itzi_cloud_push(argparse.Namespace(project=None, force=False, config_file=["sim.ini"]))

    assert warnings == ["Failed to save metadata: boom"]


def test_itzi_cloud_push_warns_when_submission_fails(monkeypatch):
    warnings = []
    request_data = SimpleNamespace(dataset_hash="hash-123")
    grass_params = GrassParams(grassdata="/db", location="loc", mapset="mapset")
    install_stub_module(
        monkeypatch,
        "itzi.cloud.auth",
        check_login=lambda: "user@example.com",
        get_token=lambda email: "token-123",
    )
    install_stub_module(
        monkeypatch,
        "itzi.cloud.push",
        create_request=lambda project, conf_file, force: (
            request_data,
            "/tmp/input.tgz",
            grass_params,
        ),
        request_simulation=lambda session_token, metadata: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
        upload_input=lambda **kwargs: pytest.fail("upload should not run"),
        confirm_upload=lambda **kwargs: pytest.fail("confirm should not run"),
    )
    install_stub_module(
        monkeypatch,
        "itzi.cloud.metadata_storage",
        save_simulation_metadata=lambda **kwargs: pytest.fail("metadata should not be saved"),
    )
    monkeypatch.setattr("itzi.cloud.cli.msgr.warning", warnings.append)

    itzi_cloud_push(argparse.Namespace(project=None, force=False, config_file=["sim.ini"]))

    assert warnings == ["sim.ini: Error during cloud submission: boom"]


def test_itzi_cloud_status_displays_single_simulation(monkeypatch):
    calls = {"get_simulation": [], "display": []}
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
        get_simulation=lambda session_token, fingerprint: (
            calls["get_simulation"].append((session_token, fingerprint))
            or {"fingerprint": fingerprint}
        ),
        get_simulations_list=lambda **kwargs: pytest.fail("list should not run"),
        display_simulations_list=lambda tasks: calls["display"].append(tasks),
    )

    itzi_cloud_status(argparse.Namespace(fingerprint="fp-123"))

    assert os.environ["ITZI_VERBOSE"] == str(VerbosityLevel.MESSAGE)
    assert calls["get_simulation"] == [("token-123", "fp-123")]
    assert calls["display"] == [[{"fingerprint": "fp-123"}]]


def test_itzi_cloud_status_displays_simulation_list(monkeypatch):
    calls = {"get_simulations_list": [], "display": []}
    install_stub_module(
        monkeypatch,
        "itzi.cloud.auth",
        check_login=lambda: "user@example.com",
        get_token=lambda email: "token-123",
    )
    install_stub_module(
        monkeypatch,
        "itzi.cloud.status",
        get_simulation=lambda **kwargs: pytest.fail("single lookup should not run"),
        get_simulations_list=lambda session_token: (
            calls["get_simulations_list"].append(session_token) or [{"fingerprint": "fp-123"}]
        ),
        display_simulations_list=lambda tasks: calls["display"].append(tasks),
    )

    itzi_cloud_status(argparse.Namespace(fingerprint=None))

    assert calls["get_simulations_list"] == ["token-123"]
    assert calls["display"] == [[{"fingerprint": "fp-123"}]]


def test_itzi_cloud_pull_downloads_and_loads_results(monkeypatch):
    calls = {"get_simulation_results_url": [], "pull_simulation_results": []}
    messages = []
    verbose_messages = []
    grass_params = GrassParams(grassdata="/db", location="loc", mapset="mapset")
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
        get_simulation_results_url=lambda session_token, fingerprint: (
            calls["get_simulation_results_url"].append((session_token, fingerprint))
            or {"download_url": "https://example.test/results"}
        ),
        pull_simulation_results=lambda download_url, grass_params, overwrite: calls[
            "pull_simulation_results"
        ].append((download_url, grass_params, overwrite)),
    )
    monkeypatch.setattr(
        "itzi.cloud.cli.resolve_cloud_pull_grass_params",
        lambda cli_args: (grass_params, "stored metadata"),
    )
    monkeypatch.setattr("itzi.cloud.cli.msgr.message", messages.append)
    monkeypatch.setattr("itzi.cloud.cli.msgr.verbose", verbose_messages.append)

    itzi_cloud_pull(
        argparse.Namespace(
            fingerprint="fp-123",
            overwrite=True,
            gisdb=None,
            project=None,
            mapset=None,
        )
    )

    assert os.environ["ITZI_VERBOSE"] == str(VerbosityLevel.MESSAGE)
    assert messages == [
        "Retrieving results for simulation fp-123...",
        "Loading results to GRASS database using stored metadata",
    ]
    assert verbose_messages == ["  Location: /db/loc/mapset"]
    assert calls["get_simulation_results_url"] == [("token-123", "fp-123")]
    assert calls["pull_simulation_results"] == [
        ("https://example.test/results", grass_params, True)
    ]


def test_resolve_cloud_pull_grass_params_prefers_active_session(monkeypatch):
    session_params = GrassParams(grassdata="/db", location="loc", mapset="mapset")
    monkeypatch.setattr("itzi.cloud.grass_utils.get_active_grass_params", lambda: session_params)
    monkeypatch.setattr(
        "itzi.cloud.metadata_storage.load_simulation_metadata",
        lambda fingerprint: pytest.fail("metadata should not be consulted"),
    )

    grass_params, source = resolve_cloud_pull_grass_params(
        argparse.Namespace(
            fingerprint="fp-123",
            gisdb=None,
            project=None,
            mapset=None,
        )
    )

    assert grass_params == session_params
    assert source == "active GRASS session"


def test_resolve_cloud_pull_grass_params_uses_cli_args_when_complete(monkeypatch):
    monkeypatch.setattr("itzi.cloud.grass_utils.get_active_grass_params", lambda: None)
    monkeypatch.setattr(
        "itzi.cloud.metadata_storage.load_simulation_metadata",
        lambda fingerprint: pytest.fail("metadata should not be consulted"),
    )

    grass_params, source = resolve_cloud_pull_grass_params(
        argparse.Namespace(
            fingerprint="fp-123",
            gisdb="/db",
            project="loc",
            mapset="mapset",
        )
    )

    assert grass_params == GrassParams(
        grassdata="/db",
        location="loc",
        mapset="mapset",
        region=None,
        mask=None,
        grass_bin=None,
    )
    assert source == "CLI arguments"


def test_resolve_cloud_pull_grass_params_falls_back_to_metadata(monkeypatch):
    metadata_params = GrassParams(grassdata="/meta", location="proj", mapset="ms")
    monkeypatch.setattr("itzi.cloud.grass_utils.get_active_grass_params", lambda: None)
    monkeypatch.setattr(
        "itzi.cloud.metadata_storage.load_simulation_metadata",
        lambda fingerprint: metadata_params,
    )

    grass_params, source = resolve_cloud_pull_grass_params(
        argparse.Namespace(
            fingerprint="fp-123",
            gisdb=None,
            project=None,
            mapset=None,
        )
    )

    assert grass_params == metadata_params
    assert source == "stored metadata"


def test_resolve_cloud_pull_grass_params_rejects_partial_cli_override(monkeypatch):
    monkeypatch.setattr("itzi.cloud.grass_utils.get_active_grass_params", lambda: None)
    monkeypatch.setattr(
        "itzi.cloud.cli.msgr.fatal",
        lambda message: (_ for _ in ()).throw(ItziFatal(message)),
    )

    with pytest.raises(ItziFatal, match="all three are required"):
        resolve_cloud_pull_grass_params(
            argparse.Namespace(
                fingerprint="fp-123",
                gisdb="/db",
                project="loc",
                mapset=None,
            )
        )


def test_resolve_cloud_pull_grass_params_requires_any_source(monkeypatch):
    monkeypatch.setattr("itzi.cloud.grass_utils.get_active_grass_params", lambda: None)
    monkeypatch.setattr(
        "itzi.cloud.metadata_storage.load_simulation_metadata",
        lambda fingerprint: None,
    )
    monkeypatch.setattr(
        "itzi.cloud.cli.msgr.fatal",
        lambda message: (_ for _ in ()).throw(ItziFatal(message)),
    )

    with pytest.raises(ItziFatal, match="Could not determine GRASS parameters"):
        resolve_cloud_pull_grass_params(
            argparse.Namespace(
                fingerprint="fp-123",
                gisdb=None,
                project=None,
                mapset=None,
            )
        )
