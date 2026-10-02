"""Provider-free cloud login, status, and pull tests."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import tarfile
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlparse

import pytest

from itzi.cloud.cli import itzi_cloud_login, itzi_cloud_pull, itzi_cloud_push, itzi_cloud_status
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
        self.ensemble_creates: list[tuple[str, str, str, bytes]] = []
        self.ensembles_by_key: dict[str, dict[str, str]] = {}
        self.create_conflict = False
        self.lose_create_response = False
        self.upload_instruction_requests: list[tuple[str, dict[str, Any], str]] = []
        self.uploads: list[tuple[str, bytes, dict[str, str]]] = []
        self.confirm_requests: list[tuple[str, dict[str, Any], str]] = []
        self.transfers: dict[str, str] = {}
        self.confirmed: dict[str, dict[str, Any]] = {}
        self.storage_entitlement_error = False
        self.expired_instructions = False
        self.put_error = False
        self.lose_put_response = False
        self.confirm_error = False
        self.lose_confirm_response = False
        self.instruction_conflict = False
        self.member_labels: dict[str, list[str]] = {}
        self.validation_state: str | None = None
        self.simulation_puts: list[tuple[str, int, dict[str, Any], str]] = []
        self.run_requests: list[tuple[str, str, str]] = []
        self.unsealed_run_requests = 0
        self.run_errors: dict[str, tuple[int, dict[str, str]]] = {}
        self.fail_simulation: int | None = None
        self.fail_run: int | None = None
        self.lose_simulation_response = False
        self.lose_run_response = False


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
        if path.startswith("/execution-api/v1/simulations/") and path.endswith("/runs"):
            if not self._require_token():
                return
            simulation_id = path.removeprefix("/execution-api/v1/simulations/").removesuffix(
                "/runs"
            )
            key = self.headers.get("Idempotency-Key", "")
            self.server.state.run_requests.append(
                (simulation_id, key, self.headers.get("X-Session-Token", ""))
            )
            if self.server.state.unsealed_run_requests:
                self.server.state.unsealed_run_requests -= 1
                self._send_json(
                    409,
                    {
                        "detail": "The Ensemble result repository must be sealed.",
                        "code": "result_repository_not_sealed",
                        "next_action": "wait_for_result_repository",
                    },
                )
                return
            if simulation_id in self.server.state.run_errors:
                status_code, payload = self.server.state.run_errors[simulation_id]
                self._send_json(status_code, payload)
                return
            if self.server.state.fail_run is not None and simulation_id.endswith(
                f"-{self.server.state.fail_run}"
            ):
                self._send_json(503, {"detail": "Run unavailable"})
                return
            run_id = f"run-{simulation_id}"
            if self.server.state.lose_run_response:
                self.server.state.lose_run_response = False
                self._send_json(503, {"detail": "Run response lost"})
                return
            self._send_json(200, {"simulation_id": simulation_id, "run_id": run_id})
            return
        if path.startswith("/execution-api/v1/inputs/"):
            if not self._require_token():
                return
            input_id, _, action = path.removeprefix("/execution-api/v1/inputs/").partition("/")
            payload = self._read_json()
            if action == "upload-instructions":
                self.server.state.upload_instruction_requests.append(
                    (input_id, payload, self.headers.get("X-Session-Token", ""))
                )
                if self.server.state.storage_entitlement_error:
                    self._send_json(
                        429,
                        {
                            "detail": "Not enough storage",
                            "required_bytes": 100,
                            "available_bytes": 5,
                        },
                    )
                    return
                if (
                    self.server.state.instruction_conflict
                    or input_id in self.server.state.confirmed
                ):
                    self._send_json(409, {"detail": "New upload instructions not permitted"})
                    return
                transfer_id = f"transfer-{len(self.server.state.upload_instruction_requests)}"
                self.server.state.transfers[input_id] = transfer_id
                expiry = datetime.now(UTC) + timedelta(
                    seconds=-10 if self.server.state.expired_instructions else 900
                )
                self._send_json(
                    200,
                    {
                        "input_id": input_id,
                        "transfer_id": transfer_id,
                        "method": "PUT",
                        "url": f"{self.server.base_url}/uploads/{transfer_id}",
                        "headers": {
                            "Content-MD5": payload["content_md5_base64"],
                            "x-upload-token": "signed-only",
                        },
                        "expires_at": expiry.isoformat(),
                        "size_bytes": payload["size_bytes"],
                        "content_type": "application/zstd",
                    },
                )
                return
            if action == "confirm":
                self.server.state.confirm_requests.append(
                    (input_id, payload, self.headers.get("X-Session-Token", ""))
                )
                if self.server.state.confirm_error:
                    self._send_json(503, {"detail": "Confirmation unavailable"})
                    return
                transfer_id = self.server.state.transfers.get(input_id)
                uploaded = next(
                    (body for key, body, _ in self.server.state.uploads if key == transfer_id),
                    None,
                )
                if (
                    uploaded is None
                    or payload["transfer_id"] != transfer_id
                    or payload["size_bytes"] != len(uploaded)
                    or payload["sha256"] != hashlib.sha256(uploaded).hexdigest()
                ):
                    self._send_json(422, {"detail": "Upload digest or transfer mismatch"})
                    return
                self.server.state.confirmed[input_id] = payload
                if self.server.state.lose_confirm_response:
                    self.server.state.lose_confirm_response = False
                    self._send_json(503, {"detail": "Response lost after confirmation"})
                    return
                self._send_json(
                    200,
                    {
                        "input_id": input_id,
                        "state": "validating",
                        "state_changed_at": datetime.now(UTC).isoformat(),
                    },
                )
                return
        if path.startswith("/execution-api/v1/projects/") and path.endswith("/ensembles"):
            if not self._require_token():
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            key = self.headers.get("Idempotency-Key", "")
            self.server.state.ensemble_creates.append(
                (path, key, self.headers.get("X-Session-Token", ""), body)
            )
            if self.server.state.create_conflict:
                self._send_json(409, {"detail": "Project cannot accept a new Ensemble"})
                return
            created = self.server.state.ensembles_by_key.setdefault(
                key,
                {
                    "ensemble_id": f"ensemble-{len(self.server.state.ensembles_by_key) + 1}",
                    "input_id": f"input-{len(self.server.state.ensembles_by_key) + 1}",
                },
            )
            if self.server.state.lose_create_response:
                self.server.state.lose_create_response = False
                self._send_json(503, {"detail": "Response lost after create"})
                return
            self._send_json(201, created)
            return
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
        if path.startswith("/execution-api/v1/inputs/"):
            if not self._require_token():
                return
            input_id = path.removeprefix("/execution-api/v1/inputs/")
            confirmed = self.server.state.confirmed.get(input_id)
            self._send_json(
                200,
                {
                    "input_id": input_id,
                    "state": (self.server.state.validation_state or "accepted")
                    if confirmed
                    else "draft",
                    "upload_confirmation": (
                        {"size_bytes": confirmed["size_bytes"], "sha256": confirmed["sha256"]}
                        if confirmed
                        else None
                    ),
                    "failure": (
                        {"message": "Archive rejected", "next_action": "fix the archive"}
                        if confirmed and self.server.state.validation_state == "failed"
                        else None
                    ),
                    "acceptance": (
                        {
                            "member_mapping": {
                                "members": [
                                    {"index": index, "label": label}
                                    for index, label in enumerate(
                                        self.server.state.member_labels.get(input_id, ["member-0"])
                                    )
                                ]
                            }
                        }
                        if confirmed and self.server.state.validation_state in (None, "accepted")
                        else None
                    ),
                },
            )
            return
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

    def do_PUT(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/execution-api/v1/inputs/") and "/simulations/" in path:
            if not self._require_token():
                return
            input_id, index_text = path.removeprefix("/execution-api/v1/inputs/").split(
                "/simulations/"
            )
            index = int(index_text)
            payload = self._read_json()
            self.server.state.simulation_puts.append(
                (input_id, index, payload, self.headers.get("X-Session-Token", ""))
            )
            if self.server.state.fail_simulation == index:
                self._send_json(503, {"detail": "Simulation unavailable"})
                return
            simulation_id = f"simulation-{input_id}-{index}"
            if self.server.state.lose_simulation_response:
                self.server.state.lose_simulation_response = False
                self._send_json(503, {"detail": "Simulation response lost"})
                return
            self._send_json(
                201,
                {
                    "simulation_id": simulation_id,
                    "input_id": input_id,
                    "ensemble_id": input_id.replace("input-", "ensemble-"),
                    "member_index": index,
                    "member_label": self.server.state.member_labels.get(input_id, ["member-0"])[
                        index
                    ],
                },
            )
            return
        if path.startswith("/uploads/"):
            transfer_id = path.removeprefix("/uploads/")
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {key.lower(): value for key, value in self.headers.items()}
            if self.server.state.put_error:
                self._send_json(503, {"detail": "Upload failed"})
                return
            if (
                transfer_id not in self.server.state.transfers.values()
                or any(key == transfer_id for key, _, _ in self.server.state.uploads)
                or headers.get("content-type") != "application/zstd"
                or headers.get("x-upload-token") != "signed-only"
                or headers.get("content-md5")
                != base64.b64encode(hashlib.md5(body).digest()).decode()
                or "x-session-token" in headers
            ):
                self._send_json(409, {"detail": "Invalid or reused signed URL"})
                return
            self.server.state.uploads.append((transfer_id, body, headers))
            if self.server.state.lose_put_response:
                self.server.state.lose_put_response = False
                self._send_json(503, {"detail": "Response lost after PUT"})
                return
            self._send_bytes(201, b"")
            return
        self._send_json(404, {"detail": f"Unhandled PUT {path}"})

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


def _fake_archive(
    path: Path,
    config: Path,
    grass_params: GrassParams,
    *,
    document_index: int = 0,
    members: int = 1,
    time: dict[str, str] | None = None,
    outputs: dict[str, Any] | None = None,
    yaml_sha256: str | None = None,
) -> SimpleNamespace:
    from itzi_core import SurfaceFlowParameters

    from itzi.ensemble.models import SourceDocument
    from itzi.ensemble.schema import YamlEnsembleDocumentV1
    from itzi.ensemble.yaml import expand_yaml_document

    ensemble = expand_yaml_document(
        SourceDocument(
            config,
            document_index,
            yaml_sha256 or hashlib.sha256(f"study-{document_index}".encode()).hexdigest(),
        ),
        YamlEnsembleDocumentV1.model_validate(
            {
                "schema_version": 1,
                "ensemble": {"id": f"study-{document_index}"},
                "grass": {},
                "time": time or {"duration": "00:10:00", "record_step": "00:05:00"},
                "input": {"ground_elevation": "dem", "friction": "n"},
                "parameters": {},
                "outputs": outputs or {},
            }
        ),
    )
    expanded = ensemble.simulations[0]
    return SimpleNamespace(
        ensemble=SimpleNamespace(
            source=ensemble.source,
            simulations=(expanded,) * members,
        ),
        simulations=tuple(
            SimpleNamespace(
                simulation_id=f"member-{document_index}-{index}" if members > 1 else "member-0",
                grass_params=grass_params,
                input_kinds=(("ground_elevation", "raster"), ("friction", "raster")),
                simulation_config=SimpleNamespace(
                    input_map_names={
                        "ground_elevation": f"dem_{index}@mapset",
                        "friction": "n@mapset",
                    },
                    surface_flow_parameters=SurfaceFlowParameters(),
                    dtinf=1.0,
                    infiltration_model="null",
                ),
            )
            for index in range(members)
        ),
        path=path,
        size_bytes=path.stat().st_size,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


@pytest.mark.cloud
def test_cloud_push_creates_and_resumes_each_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_cloud_server: FakeCloudServer
) -> None:
    from itzi.cloud import archive

    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    itzi_cloud_login(
        argparse.Namespace(email="user@example.com", password="secret", logout=False, status=False)
    )
    config = tmp_path / "study.yaml"
    payloads = (b"archive 0", b"archive 1")
    for index, payload in enumerate(payloads):
        (tmp_path / f"input-{index}.tzst").write_bytes(payload)
    archives = tuple(
        _fake_archive(
            tmp_path / f"input-{index}.tzst",
            config,
            ctx.grass_params,
            document_index=index,
            members=2,
        )
        for index in range(2)
    )
    for index in range(2):
        fake_cloud_server.state.member_labels[f"input-{index + 1}"] = [
            f"member-{index}-0",
            f"member-{index}-1",
        ]
    monkeypatch.setattr(archive, "build_archives", lambda path, token: archives)
    args = argparse.Namespace(project="proj-public", config_file=[str(config)])
    itzi_cloud_push(args)

    stored = json.loads(ctx.metadata_storage.get_metadata_file_path().read_text())
    assert "fp-001" in stored["simulations"]
    drafts = list(stored["ensembles"].values())
    assert [(draft["ensemble_id"], draft["input_id"]) for draft in drafts] == [
        ("ensemble-1", "input-1"),
        ("ensemble-2", "input-2"),
    ]
    for index, draft in enumerate(drafts):
        assert draft["project_id"] == "proj-public"
        assert draft["member_labels"] == [f"member-{index}-0", f"member-{index}-1"]
        assert draft["grass_params"]["grassdata"] == str(ctx.grass_params.grassdata)
        assert draft["grass_params"]["location"] == "project"
        assert draft["grass_params"]["mapset"] == "mapset"
    assert [call[0] for call in fake_cloud_server.state.ensemble_creates] == [
        "/execution-api/v1/projects/proj-public/ensembles"
    ] * 2
    assert [call[1] for call in fake_cloud_server.state.ensemble_creates] == [
        draft["idempotency_key"] for draft in drafts
    ]
    keys = [draft["idempotency_key"] for draft in drafts] + [
        key for _, key, _ in fake_cloud_server.state.run_requests
    ]
    assert len(keys) == len(set(keys))
    assert all(
        call[2] == "token-1" and call[3] == b""
        for call in fake_cloud_server.state.ensemble_creates
    )
    assert [body for _, body, _ in fake_cloud_server.state.uploads] == list(payloads)
    assert [item[1] for item in fake_cloud_server.state.upload_instruction_requests] == [
        {
            "size_bytes": len(payload),
            "content_md5_base64": base64.b64encode(hashlib.md5(payload).digest()).decode(),
        }
        for payload in payloads
    ]
    assert [item[1] for item in fake_cloud_server.state.confirm_requests] == [
        {
            "transfer_id": f"transfer-{index + 1}",
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for index, payload in enumerate(payloads)
    ]
    assert all(draft["upload_stage"] == "confirmed" for draft in drafts)
    assert all(draft["confirmation_state"] == "validating" for draft in drafts)
    assert [draft["run_ids"] for draft in drafts] == [
        {str(i): f"run-simulation-input-{index + 1}-{i}" for i in range(2)} for index in range(2)
    ]
    assert all(
        payload["output_configuration"]["selected_output_codes"] == ["water_depth"]
        for _, _, payload, _ in fake_cloud_server.state.simulation_puts
    )
    assert all(
        token == "token-1"
        for _, _, token in (
            *fake_cloud_server.state.upload_instruction_requests,
            *fake_cloud_server.state.confirm_requests,
        )
    )

    itzi_cloud_push(args)
    assert len(fake_cloud_server.state.ensemble_creates) == 2
    assert len(fake_cloud_server.state.uploads) == 2
    assert len(fake_cloud_server.state.confirm_requests) == 2
    assert (
        json.loads(ctx.metadata_storage.get_metadata_file_path().read_text())["ensembles"]
        == stored["ensembles"]
    )

    source = archives[0].ensemble.source
    archives[0].ensemble.source = replace(source, path=tmp_path / "moved.yaml", document_index=7)
    monkeypatch.setattr(archive, "build_archives", lambda path, token: (archives[0],))
    itzi_cloud_push(
        argparse.Namespace(project="proj-public", config_file=[str(tmp_path / "moved.yaml")])
    )
    assert len(fake_cloud_server.state.ensemble_creates) == 2
    assert (
        json.loads(ctx.metadata_storage.get_metadata_file_path().read_text())["ensembles"]
        == stored["ensembles"]
    )

    archives[0].ensemble.source = source
    monkeypatch.setattr(archive, "build_archives", lambda path, token: archives)
    archives[0].sha256 = "0" * 64
    with pytest.raises(FatalError, match="use --force"):
        itzi_cloud_push(args)
    assert len(fake_cloud_server.state.ensemble_creates) == 2


@pytest.mark.cloud
def test_cloud_push_distinguishes_yaml_and_forced_archive_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_cloud_server: FakeCloudServer
) -> None:
    from itzi.cloud import archive

    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    itzi_cloud_login(
        argparse.Namespace(email="user@example.com", password="secret", logout=False, status=False)
    )
    config = tmp_path / "study.yaml"
    path = tmp_path / "input.tzst"
    path.write_bytes(b"first Input")
    built = _fake_archive(path, config, ctx.grass_params)
    monkeypatch.setattr(archive, "build_archives", lambda *_: (built,))
    args = argparse.Namespace(project="proj-public", config_file=[str(config)], force=False)
    forced = argparse.Namespace(project="proj-public", config_file=[str(config)], force=True)

    itzi_cloud_push(args)
    path.write_bytes(b"updated Input")
    built = _fake_archive(path, config, ctx.grass_params)
    with pytest.raises(FatalError, match="use --force"):
        itzi_cloud_push(args)
    assert len(fake_cloud_server.state.ensemble_creates) == 1

    fake_cloud_server.state.lose_create_response = True
    with pytest.raises(FatalError, match="503"):
        itzi_cloud_push(forced)
    pending = list(
        json.loads(ctx.metadata_storage.get_metadata_file_path().read_text())["ensembles"].values()
    )[-1]
    assert pending["ensemble_id"] is None
    itzi_cloud_push(forced)
    itzi_cloud_push(forced)
    itzi_cloud_push(args)
    assert len(fake_cloud_server.state.ensemble_creates) == 3
    assert fake_cloud_server.state.ensemble_creates[1][1] == pending["idempotency_key"]
    assert fake_cloud_server.state.ensemble_creates[2][1] == pending["idempotency_key"]
    assert len(fake_cloud_server.state.ensembles_by_key) == 2
    assert len(fake_cloud_server.state.uploads) == 2

    built = _fake_archive(path, config, ctx.grass_params, yaml_sha256="changed YAML document")
    itzi_cloud_push(args)
    assert len(fake_cloud_server.state.ensemble_creates) == 4
    drafts = json.loads(ctx.metadata_storage.get_metadata_file_path().read_text())["ensembles"]
    assert len(drafts) == 3
    assert len({draft["idempotency_key"] for draft in drafts.values()}) == 3


@pytest.mark.cloud
@pytest.mark.parametrize("failure", ["conflict", "lost_response"])
def test_cloud_push_retries_with_same_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
    failure: str,
) -> None:
    from itzi.cloud import archive

    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    itzi_cloud_login(
        argparse.Namespace(email="user@example.com", password="secret", logout=False, status=False)
    )
    config = tmp_path / "study.yaml"
    path = tmp_path / "input.tzst"
    path.write_bytes(b"retry archive")
    monkeypatch.setattr(
        archive,
        "build_archives",
        lambda filename, token: (_fake_archive(path, config, ctx.grass_params),),
    )
    state = fake_cloud_server.state
    state.create_conflict = failure == "conflict"
    state.lose_create_response = failure == "lost_response"
    args = argparse.Namespace(project="proj-public", config_file=[str(config)])
    with pytest.raises(
        FatalError, match="Ensemble creation conflict" if failure == "conflict" else "503"
    ):
        itzi_cloud_push(args)
    metadata_path = ctx.metadata_storage.get_metadata_file_path()
    pending = next(iter(json.loads(metadata_path.read_text())["ensembles"].values()))
    assert pending["ensemble_id"] is None and pending["input_id"] is None
    assert len(state.ensembles_by_key) == (0 if failure == "conflict" else 1)

    state.create_conflict = False
    itzi_cloud_push(args)
    assert [call[1] for call in state.ensemble_creates] == [pending["idempotency_key"]] * 2
    assert len(state.ensembles_by_key) == 1
    resumed = next(iter(json.loads(metadata_path.read_text())["ensembles"].values()))
    assert (resumed["ensemble_id"], resumed["input_id"]) == ("ensemble-1", "input-1")
    itzi_cloud_push(args)
    assert len(state.ensemble_creates) == 2


@pytest.mark.cloud
@pytest.mark.parametrize(
    ("failure", "stage", "message"),
    [
        ("storage_entitlement_error", None, "storage entitlement exceeded"),
        ("expired_instructions", None, "Signed upload URL expired"),
        ("put_error", None, "Signed upload failed"),
        ("lose_put_response", None, "Signed upload failed"),
        ("confirm_error", "uploaded", "503"),
        ("lose_confirm_response", "uploaded", "503"),
    ],
)
def test_cloud_upload_failure_resumes_safely(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
    failure: str,
    stage: str | None,
    message: str,
) -> None:
    from itzi.cloud import archive

    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    itzi_cloud_login(
        argparse.Namespace(email="user@example.com", password="secret", logout=False, status=False)
    )
    config = tmp_path / "study.yaml"
    path = tmp_path / "input.tzst"
    path.write_bytes(b"archive with exact bytes")
    built = _fake_archive(path, config, ctx.grass_params)
    monkeypatch.setattr(archive, "build_archives", lambda filename, token: (built,))
    state = fake_cloud_server.state
    setattr(state, failure, True)
    args = argparse.Namespace(project="proj-public", config_file=[str(config)])
    with pytest.raises(FatalError, match=message):
        itzi_cloud_push(args)
    metadata_path = ctx.metadata_storage.get_metadata_file_path()
    draft = next(iter(json.loads(metadata_path.read_text())["ensembles"].values()))
    assert draft["upload_stage"] == stage
    assert draft["ensemble_id"] == "ensemble-1"
    assert (len(state.uploads), len(state.confirm_requests)) == {
        "storage_entitlement_error": (0, 0),
        "expired_instructions": (0, 0),
        "put_error": (0, 0),
        "lose_put_response": (1, 0),
        "confirm_error": (1, 1),
        "lose_confirm_response": (1, 1),
    }[failure]

    setattr(state, failure, False)
    itzi_cloud_push(args)
    assert len(state.ensemble_creates) == 1
    assert len(state.uploads) == (2 if failure == "lose_put_response" else 1)
    assert len({transfer for transfer, _, _ in state.uploads}) == len(state.uploads)
    assert len(state.confirm_requests) == (2 if failure == "confirm_error" else 1)
    assert len(state.upload_instruction_requests) == (
        2
        if failure
        in ("storage_entitlement_error", "expired_instructions", "put_error", "lose_put_response")
        else 1
    )
    assert (
        next(iter(json.loads(metadata_path.read_text())["ensembles"].values()))["upload_stage"]
        == "confirmed"
    )


@pytest.mark.cloud
def test_cloud_upload_refuses_unpermitted_refresh_and_wrong_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_cloud_server: FakeCloudServer
) -> None:
    from itzi.cloud import archive

    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    itzi_cloud_login(
        argparse.Namespace(email="user@example.com", password="secret", logout=False, status=False)
    )
    path = tmp_path / "input.tzst"
    path.write_bytes(b"real archive")
    built = _fake_archive(path, tmp_path / "study.yaml", ctx.grass_params)
    monkeypatch.setattr(archive, "build_archives", lambda filename, token: (built,))
    args = argparse.Namespace(project="proj-public", config_file=["study.yaml"])
    fake_cloud_server.state.expired_instructions = True
    with pytest.raises(FatalError, match="Signed upload URL expired"):
        itzi_cloud_push(args)
    assert fake_cloud_server.state.confirmed == {}
    assert len(fake_cloud_server.state.uploads) == 0
    fake_cloud_server.state.expired_instructions = False
    path.write_bytes(b"fake archive")
    with pytest.raises(FatalError, match="Archive SHA-256 changed"):
        itzi_cloud_push(args)
    path.write_bytes(b"real archive")

    # A retry must not reuse the expired URL when the service refuses fresh instructions.
    fake_cloud_server.state.instruction_conflict = True
    with pytest.raises(FatalError, match="409"):
        itzi_cloud_push(args)
    assert len(fake_cloud_server.state.uploads) == 0


@pytest.mark.cloud
@pytest.mark.parametrize("validation_state", ["validating", "failed", "accepted"])
def test_cloud_push_waits_for_accepted_member_mapping(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
    validation_state: str,
) -> None:
    from itzi.cloud import archive, push

    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    itzi_cloud_login(
        argparse.Namespace(email="user@example.com", password="secret", logout=False, status=False)
    )
    path = tmp_path / "input.tzst"
    path.write_bytes(b"input")
    built = _fake_archive(path, tmp_path / "study.yaml", ctx.grass_params)
    monkeypatch.setattr(archive, "build_archives", lambda *_: (built,))
    monkeypatch.setattr(push, "VALIDATION_WAIT_SECONDS", 0)
    state = fake_cloud_server.state
    state.validation_state = validation_state
    if validation_state == "accepted":
        state.member_labels["input-1"] = ["wrong-label"]
    with pytest.raises(
        FatalError,
        match={
            "validating": "validation still pending.*Retry cloud push",
            "failed": "Archive rejected; next action: fix the archive.*new Ensemble",
            "accepted": "member mapping differs from local order",
        }[validation_state],
    ):
        itzi_cloud_push(
            argparse.Namespace(
                project="proj-public", config_file=[str(built.ensemble.source.path)]
            )
        )
    assert not state.simulation_puts and not state.run_requests
    state.validation_state = "accepted"
    state.member_labels["input-1"] = ["member-0"]
    itzi_cloud_push(
        argparse.Namespace(project="proj-public", config_file=[str(built.ensemble.source.path)])
    )
    assert len(state.uploads) == len(state.confirm_requests) == len(state.ensemble_creates) == 1
    assert len(state.simulation_puts) == len(state.run_requests) == 1


@pytest.mark.cloud
@pytest.mark.parametrize(
    "failure", ["fail_simulation", "lose_simulation_response", "fail_run", "lose_run_response"]
)
def test_cloud_push_resumes_failed_member_and_replays_run_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
    failure: str,
) -> None:
    from itzi.cloud import archive

    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    itzi_cloud_login(
        argparse.Namespace(email="user@example.com", password="secret", logout=False, status=False)
    )
    path = tmp_path / "input.tzst"
    path.write_bytes(b"input")
    built = _fake_archive(path, tmp_path / "study.yaml", ctx.grass_params, members=2)
    monkeypatch.setattr(archive, "build_archives", lambda *_: (built,))
    state = fake_cloud_server.state
    state.member_labels["input-1"] = ["member-0-0", "member-0-1"]
    setattr(state, failure, 0 if failure.startswith("fail_") else True)
    args = argparse.Namespace(project="proj-public", config_file=[str(built.ensemble.source.path)])
    with pytest.raises(FatalError, match="member 0.*Retry cloud push"):
        itzi_cloud_push(args)
    metadata_path = ctx.metadata_storage.get_metadata_file_path()
    draft = next(iter(json.loads(metadata_path.read_text())["ensembles"].values()))
    assert draft["run_ids"] == {"1": "run-simulation-input-1-1"}
    assert len(state.ensemble_creates) == len(state.uploads) == 1

    setattr(state, failure, None if failure.startswith("fail_") else False)
    itzi_cloud_push(args)
    draft = next(iter(json.loads(metadata_path.read_text())["ensembles"].values()))
    assert set(draft["simulation_ids"]) == set(draft["run_ids"]) == {"0", "1"}
    assert len(state.ensemble_creates) == len(state.uploads) == len(state.confirm_requests) == 1
    assert [index for _, index, _, _ in state.simulation_puts].count(1) == 1
    assert [sim for sim, _, _ in state.run_requests].count("simulation-input-1-1") == 1
    if failure in ("fail_run", "lose_run_response"):
        assert [key for sim, key, _ in state.run_requests if sim == "simulation-input-1-0"] == [
            state.run_requests[0][1]
        ] * 2
    previous = (len(state.simulation_puts), len(state.run_requests))
    itzi_cloud_push(args)
    assert (len(state.simulation_puts), len(state.run_requests)) == previous


@pytest.mark.cloud
def test_cloud_push_retries_until_result_repository_is_sealed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_cloud_server: FakeCloudServer
) -> None:
    from itzi.cloud import archive, push

    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    itzi_cloud_login(
        argparse.Namespace(email="user@example.com", password="secret", logout=False, status=False)
    )
    path = tmp_path / "input.tzst"
    path.write_bytes(b"input")
    built = _fake_archive(path, tmp_path / "study.yaml", ctx.grass_params, members=2)
    monkeypatch.setattr(archive, "build_archives", lambda *_: (built,))
    monkeypatch.setattr(push, "RESULT_REPOSITORY_POLL_SECONDS", 0)
    state = fake_cloud_server.state
    state.member_labels["input-1"] = ["member-0-0", "member-0-1"]
    state.unsealed_run_requests = 2
    args = argparse.Namespace(project="proj-public", config_file=[str(built.ensemble.source.path)])

    itzi_cloud_push(args)

    assert len(state.simulation_puts) == 2
    assert [sim for sim, _, _ in state.run_requests] == ["simulation-input-1-0"] * 3 + [
        "simulation-input-1-1"
    ]
    assert len({key for _, key, _ in state.run_requests[:3]}) == 1
    assert all(token == "token-1" for _, _, token in state.run_requests)
    itzi_cloud_push(args)
    assert len(state.simulation_puts) == 2
    assert len(state.run_requests) == 4


@pytest.mark.cloud
@pytest.mark.parametrize("failure", ["pending", "other_conflict", "wrong_action", "malformed"])
def test_cloud_push_repository_failure_preserves_progress_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    from itzi.cloud import archive, push

    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    itzi_cloud_login(
        argparse.Namespace(email="user@example.com", password="secret", logout=False, status=False)
    )
    path = tmp_path / "input.tzst"
    path.write_bytes(b"input")
    built = _fake_archive(path, tmp_path / "study.yaml", ctx.grass_params, members=2)
    monkeypatch.setattr(archive, "build_archives", lambda *_: (built,))
    elapsed = 0.0
    sleeps: list[float] = []

    def advance_clock(seconds: float) -> None:
        nonlocal elapsed
        sleeps.append(seconds)
        elapsed += seconds

    monkeypatch.setattr(push.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(push.time, "sleep", advance_clock)
    state = fake_cloud_server.state
    state.member_labels["input-1"] = ["member-0-0", "member-0-1"]
    payload = {"detail": "The Ensemble result repository must be sealed."}
    if failure != "malformed":
        payload |= {
            "code": "simulation_not_eligible"
            if failure == "other_conflict"
            else "result_repository_not_sealed",
            "next_action": "create_new_simulation"
            if failure in ("other_conflict", "wrong_action")
            else "wait_for_result_repository",
        }
    state.run_errors["simulation-input-1-1"] = (409, payload)
    args = argparse.Namespace(project="proj-public", config_file=[str(built.ensemble.source.path)])
    with pytest.raises(
        FatalError,
        match="Ensemble ensemble-1.*not sealed after 10 seconds.*Retry cloud push"
        if failure == "pending"
        else "member 1.*409.*Retry cloud push",
    ):
        itzi_cloud_push(args)

    draft = next(
        iter(
            json.loads(ctx.metadata_storage.get_metadata_file_path().read_text())[
                "ensembles"
            ].values()
        )
    )
    assert set(draft["simulation_ids"]) == {"0", "1"}
    assert draft["run_ids"] == {"0": "run-simulation-input-1-0"}
    assert elapsed == (10 if failure == "pending" else 0)
    assert sleeps == ([2] * 5 if failure == "pending" else [])
    requests_before_resume = 6 if failure == "pending" else 2
    assert len(state.run_requests) == requests_before_resume
    if failure != "pending":
        assert (
            f"POST {fake_cloud_server.base_url}/execution-api/v1/simulations/"
            "simulation-input-1-1/runs: None"
        ) in capsys.readouterr().err

    state.run_errors.clear()
    itzi_cloud_push(args)
    assert len(state.simulation_puts) == 2
    assert len(state.ensemble_creates) == len(state.uploads) == len(state.confirm_requests) == 1
    assert len(state.run_requests) == requests_before_resume + 1
    assert len({key for sim, key, _ in state.run_requests if sim == "simulation-input-1-1"}) == 1


@pytest.mark.cloud
@pytest.mark.parametrize(
    ("time", "absolute_start"),
    [
        ({"duration": "00:10:00", "record_step": "00:05:00"}, None),
        (
            {
                "start": "2026-09-30T12:00:00+05:30",
                "duration": "00:10:00",
                "record_step": "00:05:00",
            },
            "2026-09-30T06:30:00+00:00",
        ),
        (
            {"start": "2026-09-30T12:00:00", "duration": "00:10:00", "record_step": "00:05:00"},
            datetime(2026, 9, 30, 12).astimezone(UTC).isoformat(),
        ),
    ],
)
def test_cloud_output_time_and_member_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
    time: dict[str, str],
    absolute_start: str | None,
) -> None:
    from itzi.cloud import archive

    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    itzi_cloud_login(
        argparse.Namespace(email="user@example.com", password="secret", logout=False, status=False)
    )
    path = tmp_path / "input.tzst"
    path.write_bytes(b"input")
    built = _fake_archive(
        path,
        tmp_path / "study.yaml",
        ctx.grass_params,
        members=2,
        time=time,
        outputs={"rasters": {"prefix": "cloud", "variables": ["water_depth", "flow_speed"]}},
    )
    monkeypatch.setattr(archive, "build_archives", lambda *_: (built,))
    fake_cloud_server.state.member_labels["input-1"] = ["member-0-0", "member-0-1"]
    itzi_cloud_push(
        argparse.Namespace(project="proj-public", config_file=[str(built.ensemble.source.path)])
    )
    puts = fake_cloud_server.state.simulation_puts
    assert [item[2]["configuration"]["input_map_names"] for item in puts] == [
        {"ground_elevation": "source_0", "friction": "source_1"},
        {"ground_elevation": "source_2", "friction": "source_1"},
    ]
    for _, _, payload, token in puts:
        assert token == "token-1"
        assert payload["output_configuration"] == {
            "selected_output_codes": ["water_depth", "flow_speed"],
            "temporal_type": "absolute" if absolute_start else "relative",
            "absolute_start_time": absolute_start,
            "start_offset_us": 0,
            "end_offset_us": 600_000_000,
            "report_interval_us": 300_000_000,
        }
        config = payload["configuration"]
        assert set(config["surface_flow_parameters"]) == {
            "hmin",
            "cfl",
            "theta",
            "g",
            "dtmax",
            "slope_threshold",
            "max_slope",
            "max_error",
        }
        assert config["dtinf"] > 0 and config["infiltration_model"] == "null"
