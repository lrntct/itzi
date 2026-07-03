"""Provider-free cloud roundtrip tests."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import io
import json
import tarfile
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

import pytest

from itzi.cloud.cli import itzi_cloud_login, itzi_cloud_pull, itzi_cloud_push, itzi_cloud_status
from itzi.cloud.schemas import DomainInfo, SimulationRequestSchema
from itzi.const import TemporalType
from itzi.data_containers import GrassParams, SimulationConfig, SurfaceFlowParameters
from itzi.itzi_error import ItziFatal


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


def _write_archive(path: Path, root_name: str, files: dict[str, bytes]) -> Path:
    path.write_bytes(_build_tar_archive(root_name, files))
    return path


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
        self.uploaded_payloads: dict[str, bytes] = {}
        self.upload_headers: dict[str, dict[str, str]] = {}
        self.download_archives: dict[str, bytes] = {}
        self.created_requests: list[dict[str, Any]] = []
        self.confirmed_fingerprints: list[str] = []
        self.next_simulation_creation_error: tuple[int, dict[str, Any]] | None = None
        self.results_lookup_errors: dict[str, tuple[int, dict[str, Any]]] = {}

    def create_simulation(self, metadata: dict[str, Any], base_url: str) -> dict[str, str]:
        fingerprint = f"fp-{len(self.simulations) + 1:03d}"
        now = datetime.now(UTC).isoformat()
        self.created_requests.append(metadata)
        self.simulations[fingerprint] = {
            "team": "integration-tests",
            "created_on": now,
            "last_updated": now,
            "fingerprint": fingerprint,
            "status": "waiting-upload",
            "progress": 50,
            "input_bytes": metadata["dataset_bytes"],
            "results_bytes": 0,
        }
        return {
            "upload_url": f"{base_url}/uploads/{fingerprint}",
            "fingerprint": fingerprint,
        }

    def confirm_upload(self, fingerprint: str) -> None:
        now = datetime.now(UTC).isoformat()
        archive = _build_tar_archive(
            "results.zarr",
            {
                "metadata.json": json.dumps({"fingerprint": fingerprint}).encode(),
                "summary.txt": b"synthetic results",
            },
        )
        self.download_archives[fingerprint] = archive
        self.confirmed_fingerprints.append(fingerprint)
        self.simulations[fingerprint].update(
            {
                "status": "completed",
                "progress": 1000,
                "results_bytes": len(archive),
                "last_updated": now,
            }
        )


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

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/_allauth/app/v1/auth/login":
            payload = self._read_json()
            email = payload["email"]
            token = f"token-{len(self.server.state.tokens_by_email) + 1}"
            self.server.state.tokens_by_email[email] = token
            self.server.state.email_by_token[token] = email
            self._send_json(200, {"meta": {"session_token": token}})
            return

        if path == "/itzi-api/simulations":
            if not self._require_token():
                return
            metadata = self._read_json()
            if self.server.state.next_simulation_creation_error is not None:
                status_code, payload = self.server.state.next_simulation_creation_error
                self.server.state.next_simulation_creation_error = None
                self._send_json(status_code, payload)
                return
            response = self.server.state.create_simulation(metadata, self.server.base_url)
            self._send_json(201, response)
            return

        fingerprint = self._match_simulation_subresource(path, "confirm-upload")
        if fingerprint is not None:
            if not self._require_token():
                return
            self.server.state.confirm_upload(fingerprint)
            self._send_json(202, {"fingerprint": fingerprint})
            return

        self._send_json(404, {"detail": f"Unhandled POST {path}"})

    def do_GET(self) -> None:  # noqa: N802
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
                {"download_url": f"{self.server.base_url}/downloads/{fingerprint}"},
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
            archive = self.server.state.download_archives.get(fingerprint)
            if archive is None:
                self._send_json(404, {"detail": "Results not found"})
                return
            self._send_bytes(200, archive, content_type="application/gzip")
            return

        self._send_json(404, {"detail": f"Unhandled GET {path}"})

    def do_PUT(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if not path.startswith("/uploads/"):
            self._send_json(404, {"detail": f"Unhandled PUT {path}"})
            return

        fingerprint = path.removeprefix("/uploads/")
        content_length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(content_length)
        expected_md5 = self.headers.get("content-md5")
        actual_md5 = base64.b64encode(hashlib.md5(payload).digest()).decode("utf-8")
        if expected_md5 != actual_md5:
            self._send_json(400, {"detail": "Content-MD5 mismatch"})
            return

        self.server.state.uploaded_payloads[fingerprint] = payload
        self.server.state.upload_headers[fingerprint] = {
            "content-md5": expected_md5 or "",
            "content-type": self.headers.get("content-type", ""),
        }
        self._send_bytes(200, b"")

    def do_DELETE(self) -> None:  # noqa: N802
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
    push: Any
    grass_params: GrassParams
    input_archive: Path
    request_data: SimulationRequestSchema


def _configure_cloud_test_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
) -> CloudTestContext:
    from itzi.cloud import auth, grass_utils, metadata_storage, pull, push

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

    input_archive = _write_archive(
        tmp_path / "input.tgz",
        "itzi_input.zarr",
        {"attrs.json": b"{}", "variables/water_level": b"placeholder"},
    )
    dataset_hash = push.md5_base64(input_archive)
    request_data = SimulationRequestSchema(
        project_id=42,
        force_rerun=True,
        sim_config=SimulationConfig(
            start_time=datetime(2025, 1, 1, 12, tzinfo=UTC),
            end_time=datetime(2025, 1, 1, 13, tzinfo=UTC),
            record_step=timedelta(minutes=15),
            temporal_type=TemporalType.ABSOLUTE,
            input_map_names={"dem": "dem"},
            output_map_names={"h": "depth"},
            surface_flow_parameters=SurfaceFlowParameters(),
        ),
        dataset_hash=dataset_hash,
        dataset_bytes=input_archive.stat().st_size,
        domain_info=DomainInfo(rows=2, cols=3, ewres=5.0, nsres=5.0),
    )
    monkeypatch.setattr(
        push,
        "create_request",
        lambda project, conf_file, force: (request_data, input_archive, grass_params),
    )

    return CloudTestContext(
        metadata_storage=metadata_storage,
        pull=pull,
        push=push,
        grass_params=grass_params,
        input_archive=input_archive,
        request_data=request_data,
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
def test_cloud_roundtrip_with_fake_provider(
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

    itzi_cloud_push(argparse.Namespace(project=42, force=True, config_file=["sim.ini"]))

    metadata_file = ctx.metadata_storage.get_metadata_file_path()
    stored_metadata = json.loads(metadata_file.read_text())
    assert stored_metadata["simulations"]["fp-001"]["grass_params"] == {
        "grassdata": str(ctx.grass_params.grassdata),
        "location": "project",
        "mapset": "mapset",
        "grass_bin": None,
    }
    assert fake_cloud_server.state.created_requests == [ctx.request_data.model_dump(mode="json")]
    assert fake_cloud_server.state.uploaded_payloads["fp-001"] == ctx.input_archive.read_bytes()
    assert fake_cloud_server.state.upload_headers["fp-001"] == {
        "content-md5": ctx.request_data.dataset_hash,
        "content-type": "application/gzip",
    }
    assert fake_cloud_server.state.confirmed_fingerprints == ["fp-001"]

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


@pytest.mark.cloud
def test_cloud_push_warns_on_conflicting_simulation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
) -> None:
    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    fake_cloud_server.state.next_simulation_creation_error = (
        409,
        {"existing_fingerprint": "fp-existing", "status": "running"},
    )

    warnings: list[str] = []
    monkeypatch.setattr("itzi.cloud.cli.msgr.warning", warnings.append)

    itzi_cloud_login(
        argparse.Namespace(
            email="user@example.com",
            password="secret",
            logout=False,
            status=False,
        )
    )

    itzi_cloud_push(argparse.Namespace(project=42, force=True, config_file=["sim.ini"]))

    assert warnings == [
        "sim.ini: Error during cloud submission: An identical simulation is already in progress. "
        "Fingerprint: fp-existing, status: running."
    ]
    assert fake_cloud_server.state.created_requests == []
    assert fake_cloud_server.state.uploaded_payloads == {}
    assert fake_cloud_server.state.confirmed_fingerprints == []
    assert ctx.metadata_storage.list_all_simulations() == {}


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
    itzi_cloud_push(argparse.Namespace(project=42, force=True, config_file=["sim.ini"]))

    fake_cloud_server.state.results_lookup_errors["fp-001"] = (
        409,
        {"detail": "Results are not available yet"},
    )

    with pytest.raises(ItziFatal, match="Results are not available yet"):
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

    with pytest.raises(ItziFatal, match="Please log in first"):
        itzi_cloud_status(argparse.Namespace(fingerprint=None))
