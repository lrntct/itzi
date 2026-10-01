"""Provider-free cloud login, status, and pull tests."""

from __future__ import annotations

import argparse
import io
import json
import tarfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import pytest

from itzi.cloud.cli import itzi_cloud_login, itzi_cloud_pull, itzi_cloud_status
from itzi.grass.session import GrassParams
from itzi.messenger import FatalError


def _build_tar_archive(root_name: str, files: dict[str, bytes]) -> bytes:
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        root_info = tarfile.TarInfo(root_name)
        root_info.type = tarfile.DIRTYPE
        root_info.mtime = 0
        tar.addfile(root_info)

        for relative_path, payload in files.items():
            file_info = tarfile.TarInfo(f"{root_name}/{relative_path}")
            file_info.size = len(payload)
            file_info.mtime = 0
            tar.addfile(file_info, io.BytesIO(payload))

    return archive.getvalue()


class InMemoryKeyring:
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self._store[(service_name, username)] = password

    def get_password(self, service_name: str, username: str) -> str | None:
        return self._store.get((service_name, username))

    def delete_password(self, service_name: str, username: str) -> None:
        self._store.pop((service_name, username), None)


class FakeCloudState:
    def __init__(self) -> None:
        self.tokens_by_email: dict[str, str] = {}
        self.email_by_token: dict[str, str] = {}
        self.simulations: dict[str, dict[str, Any]] = {}
        self.download_headers: dict[str, dict[str, str]] = {}
        self.download_archives: dict[str, bytes] = {}
        self.results_lookup_errors: dict[str, tuple[int, dict[str, Any]]] = {}


class FakeCloudServer(ThreadingHTTPServer):
    def __init__(self, state: FakeCloudState) -> None:
        super().__init__(("127.0.0.1", 0), FakeCloudRequestHandler)
        self.state = state

    @property
    def base_url(self) -> str:
        host, port = self.server_address
        return f"http://{host}:{port}"


class FakeCloudRequestHandler(BaseHTTPRequestHandler):
    server: FakeCloudServer

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/_allauth/app/v1/auth/login":
            payload = self._read_json()
            email = payload["email"]
            token = f"token-{len(self.server.state.tokens_by_email) + 1}"
            self.server.state.tokens_by_email[email] = token
            self.server.state.email_by_token[token] = email
            self._send_json(200, {"meta": {"session_token": token}})
            return

        self._send_json(404, {"detail": f"Unhandled POST {path}"})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/_allauth/app/v1/auth/session":
            token = self.headers.get("X-Session-Token")
            if token in self.server.state.email_by_token:
                self._send_json(200, {"meta": {"is_authenticated": True}})
            else:
                self._send_json(401, {"meta": {"is_authenticated": False}})
            return

        if path == "/itzi-api/simulations":
            if not self._require_token():
                return
            tasks = list(self.server.state.simulations.values())
            self._send_json(200, tasks)
            return

        fingerprint = self._match_simulation_subresource(path, "results")
        if fingerprint is not None:
            if not self._require_token():
                return
            if fingerprint in self.server.state.results_lookup_errors:
                status_code, payload = self.server.state.results_lookup_errors[fingerprint]
                self._send_json(status_code, payload)
                return
            self._send_json(
                200,
                {
                    "fingerprint": fingerprint,
                    "download_url": f"{self.server.base_url}/downloads/{fingerprint}",
                    "status": "completed",
                    "download_method": "GET",
                    "download_headers": {"x-download-token": "download-123"},
                    "download_expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
                },
            )
            return

        if path.startswith("/itzi-api/simulations/"):
            if not self._require_token():
                return
            fingerprint = path.removeprefix("/itzi-api/simulations/")
            task = self.server.state.simulations.get(fingerprint)
            if task is None:
                self._send_json(404, {"detail": "Simulation not found"})
                return
            self._send_json(200, task)
            return

        if path.startswith("/downloads/"):
            fingerprint = path.removeprefix("/downloads/")
            self.server.state.download_headers[fingerprint] = {
                "x-download-token": self.headers.get("x-download-token", "")
            }
            archive = self.server.state.download_archives.get(fingerprint)
            if archive is None:
                self._send_json(404, {"detail": "Results not found"})
                return
            self._send_bytes(200, archive, content_type="application/gzip")
            return

        self._send_json(404, {"detail": f"Unhandled GET {path}"})

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if path != "/_allauth/app/v1/auth/session":
            self._send_json(404, {"detail": f"Unhandled DELETE {path}"})
            return

        token = self.headers.get("X-Session-Token")
        if token is not None:
            email = self.server.state.email_by_token.pop(token, None)
            if email is not None:
                self.server.state.tokens_by_email.pop(email, None)
        self._send_json(401, {"meta": {"is_authenticated": False}})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _match_simulation_subresource(self, path: str, suffix: str) -> str | None:
        prefix = "/itzi-api/simulations/"
        suffix_text = f"/{suffix}"
        if path.startswith(prefix) and path.endswith(suffix_text):
            return path.removeprefix(prefix).removesuffix(suffix_text)
        return None

    def _read_json(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        if not body:
            return {}
        return cast(dict[str, Any], json.loads(body))

    def _require_token(self) -> bool:
        token = self.headers.get("X-Session-Token")
        if token in self.server.state.email_by_token:
            return True
        self._send_json(401, {"detail": "Authentication required"})
        return False

    def _send_json(self, status_code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(status_code, body, content_type="application/json")

    def _send_bytes(self, status_code: int, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


@dataclass(frozen=True)
class CloudTestContext:
    metadata_storage: Any
    pull: Any
    grass_params: GrassParams


def _configure_cloud_test_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
) -> CloudTestContext:
    from itzi.cloud import auth, grass_utils, metadata_storage, pull

    monkeypatch.setenv("ITZI_CLOUD_API_BASE", fake_cloud_server.base_url)

    keyring = InMemoryKeyring()
    monkeypatch.setattr(auth.keyring, "set_password", keyring.set_password)
    monkeypatch.setattr(auth.keyring, "get_password", keyring.get_password)
    monkeypatch.setattr(auth.keyring, "delete_password", keyring.delete_password)

    metadata_root = tmp_path / "appdata"
    monkeypatch.setattr(
        metadata_storage,
        "user_data_dir",
        lambda appname, author: str(metadata_root),
    )
    monkeypatch.setattr(grass_utils, "get_active_grass_params", lambda: None)

    grassdata = tmp_path / "grassdb"
    (grassdata / "project" / "mapset").mkdir(parents=True)
    grass_params = GrassParams(grassdata=str(grassdata), location="project", mapset="mapset")

    archive = _build_tar_archive(
        "results.zarr",
        {"metadata.json": b'{"fingerprint": "fp-001"}', "summary.txt": b"synthetic results"},
    )
    fake_cloud_server.state.download_archives["fp-001"] = archive
    now = datetime.now(UTC).isoformat()
    fake_cloud_server.state.simulations["fp-001"] = {
        "team": "integration-tests",
        "project_slug": "test-project",
        "created_on": now,
        "last_updated": now,
        "fingerprint": "fp-001",
        "status": "completed",
        "progress": 1000,
        "input_bytes": 0,
        "results_bytes": len(archive),
    }
    metadata_storage.save_simulation_metadata(
        "fp-001", "user@example.com", "sim.yaml", grass_params
    )

    return CloudTestContext(
        metadata_storage=metadata_storage,
        pull=pull,
        grass_params=grass_params,
    )


@pytest.fixture
def fake_cloud_server() -> FakeCloudServer:
    state = FakeCloudState()
    server = FakeCloudServer(state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.cloud
def test_cloud_login_status_pull_with_fake_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
) -> None:
    from itzi.cloud import status

    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)

    loaded_results: list[dict[str, Any]] = []

    def record_loaded_results(
        temp_data_path: Path, grass_params: GrassParams, overwrite: bool
    ) -> None:
        loaded_results.append(
            {
                "path": temp_data_path,
                "exists": temp_data_path.exists(),
                "metadata": (temp_data_path / "metadata.json").read_text(),
                "grass_params": grass_params,
                "overwrite": overwrite,
            }
        )

    monkeypatch.setattr(ctx.pull, "load_to_grass", record_loaded_results)

    status_messages: list[str] = []
    monkeypatch.setattr(status.msgr, "message", status_messages.append)

    itzi_cloud_login(
        argparse.Namespace(
            email="user@example.com",
            password="secret",
            logout=False,
            status=False,
        )
    )

    metadata_file = ctx.metadata_storage.get_metadata_file_path()
    stored_metadata = json.loads(metadata_file.read_text())
    assert stored_metadata["simulations"]["fp-001"]["grass_params"] == {
        "grassdata": str(ctx.grass_params.grassdata),
        "location": "project",
        "mapset": "mapset",
        "grass_bin": None,
    }
    itzi_cloud_status(argparse.Namespace(fingerprint=None))
    itzi_cloud_status(argparse.Namespace(fingerprint="fp-001"))

    assert any("FINGERPRINT" in message for message in status_messages)
    assert any("fp-001" in message and "completed" in message for message in status_messages)

    itzi_cloud_pull(
        argparse.Namespace(
            fingerprint="fp-001",
            overwrite=True,
            gisdb=None,
            project=None,
            mapset=None,
        )
    )

    assert len(loaded_results) == 1
    assert loaded_results[0]["exists"] is True
    assert loaded_results[0]["metadata"] == '{"fingerprint": "fp-001"}'
    assert loaded_results[0]["grass_params"] == ctx.grass_params
    assert loaded_results[0]["overwrite"] is True
    assert fake_cloud_server.state.download_headers["fp-001"] == {
        "x-download-token": "download-123"
    }


@pytest.mark.cloud
def test_cloud_pull_surfaces_api_detail_when_results_are_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
) -> None:
    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    monkeypatch.setattr(ctx.pull, "load_to_grass", lambda *args, **kwargs: pytest.fail())

    itzi_cloud_login(
        argparse.Namespace(
            email="user@example.com",
            password="secret",
            logout=False,
            status=False,
        )
    )
    fake_cloud_server.state.results_lookup_errors["fp-001"] = (
        409,
        {"detail": "Results are not available yet"},
    )

    with pytest.raises(FatalError, match="Results are not available yet"):
        itzi_cloud_pull(
            argparse.Namespace(
                fingerprint="fp-001",
                overwrite=False,
                gisdb=None,
                project=None,
                mapset=None,
            )
        )


@pytest.mark.cloud
def test_cloud_status_requires_an_active_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
) -> None:
    _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)

    itzi_cloud_login(
        argparse.Namespace(
            email="user@example.com",
            password="secret",
            logout=False,
            status=False,
        )
    )

    fake_cloud_server.state.tokens_by_email.clear()
    fake_cloud_server.state.email_by_token.clear()

    with pytest.raises(FatalError, match="Please log in first"):
        itzi_cloud_status(argparse.Namespace(fingerprint=None))
