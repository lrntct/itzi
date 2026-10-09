"""Provider-free cloud login, status, and pull tests."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from urllib.parse import urlparse

import pytest

from itzi.cloud.cli import itzi_cloud_login, itzi_cloud_pull, itzi_cloud_push, itzi_cloud_status
from itzi.grass.session import GrassParams
from itzi.messenger import FatalError


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
        self.download_headers: dict[str, dict[str, str]] = {}
        self.package_archive = b"opaque result package\x00" * 100000
        self.package_requests: list[tuple[str, dict[str, list[str]], str]] = []
        self.package_gets: list[tuple[str, str]] = []
        self.package_states = ["queued", "running", "ready"]
        self.package_error: tuple[int, dict[str, str]] | None = None
        self.package_status_error: tuple[int, dict[str, str]] | None = None
        self.expired_package_urls = 0
        self.download_denials = 0
        self.package_artifact_expired = False
        self.missing_package_download = False
        self.wrong_package_id = False
        self.wrong_manifest = False
        self.package_size_delta = 0
        self.wrong_package_digest = False
        self.download_status = 200
        self.interrupt_download = False
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
        self.ensemble_simulations: dict[str, list[dict[str, str | int]]] = {}
        self.status_gets: list[tuple[str, str]] = []


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
        if path.startswith("/execution-api/v1/ensembles/") and path.endswith("/packages"):
            if not self._require_token():
                return
            self.server.state.package_requests.append(
                (path, self._read_json(), self.headers.get("X-Session-Token", ""))
            )
            if self.server.state.package_error is not None:
                self._send_json(*self.server.state.package_error)
                return
            self._send_json(
                202,
                {"package_id": "package-1", "state": "queued", "manifest_sha256": "c" * 64},
            )
            return
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
        if path.startswith("/execution-api/v1/ensembles/") and "/packages/" in path:
            if not self._require_token():
                return
            state = self.server.state
            state.package_gets.append((path, self.headers.get("X-Session-Token", "")))
            if state.package_status_error is not None:
                self._send_json(*state.package_status_error)
                return
            package_state = state.package_states[0]
            if len(state.package_states) > 1:
                state.package_states.pop(0)
            url_expired = state.expired_package_urls > 0
            state.expired_package_urls = max(0, state.expired_package_urls - 1)
            now = datetime.now(UTC)
            self._send_json(
                200,
                {
                    "package_id": "wrong-package" if state.wrong_package_id else "package-1",
                    "state": package_state,
                    "manifest_sha256": "d" * 64 if state.wrong_manifest else "c" * 64,
                    "failure_code": "package_export_failed" if package_state == "failed" else "",
                    "download": (
                        {
                            "method": "GET",
                            "url": f"{self.server.base_url}/downloads/"
                            f"package-{len(state.package_gets)}",
                            "headers": {"x-download-token": "signed-only"},
                            "url_expires_at": (
                                now + timedelta(seconds=-10 if url_expired else 900)
                            ).isoformat(),
                            "artifact_expires_at": (
                                now
                                + timedelta(
                                    seconds=-10 if state.package_artifact_expired else 3600
                                )
                            ).isoformat(),
                            "size_bytes": len(state.package_archive) + state.package_size_delta,
                            "archive_sha256": (
                                "0" * 64
                                if state.wrong_package_digest
                                else hashlib.sha256(state.package_archive).hexdigest()
                            ),
                        }
                        if package_state == "ready" and not state.missing_package_download
                        else None
                    ),
                },
            )
            return
        if path.startswith("/execution-api/v1/inputs/"):
            if not self._require_token():
                return
            self.server.state.status_gets.append((path, self.headers.get("X-Session-Token", "")))
            input_id = path.removeprefix("/execution-api/v1/inputs/")
            confirmed = self.server.state.confirmed.get(input_id)
            self._send_json(
                200,
                {
                    "project_id": "project-id",
                    "ensemble_id": input_id.replace("input-", "ensemble-"),
                    "input_id": input_id,
                    "state": self.server.state.validation_state
                    or ("accepted" if confirmed else "draft"),
                    "state_changed_at": "2026-10-01T12:00:00+00:00",
                    "input_storage": {
                        "reserved_bytes": 0 if confirmed else 100,
                        "accepted_bytes": confirmed["size_bytes"] if confirmed else 0,
                    },
                    "account_storage": {
                        "entitlement_bytes": 1000,
                        "reserved_bytes": 100,
                        "accepted_bytes": confirmed["size_bytes"] if confirmed else 0,
                        "available_bytes": 900 - (confirmed["size_bytes"] if confirmed else 0),
                    },
                    "upload_confirmation": (
                        {"size_bytes": confirmed["size_bytes"], "sha256": confirmed["sha256"]}
                        if confirmed
                        else None
                    ),
                    "failure": (
                        {
                            "kind": "validation",
                            "code": "invalid_archive",
                            "message": "Archive rejected",
                            "next_action": "fix the archive",
                            "internal_traceback": "private traceback must not be displayed",
                        }
                        if confirmed and self.server.state.validation_state == "failed"
                        else None
                    ),
                    "acceptance": (
                        {
                            "contract_revision": "precipient-itzi-input-v1",
                            "accepted_at": "2026-10-01T12:00:00+00:00",
                            "crs": {"wkt": 'LOCAL_CS["metre grid"]'},
                            "extent": {
                                "minimum_x": 0,
                                "minimum_y": 0,
                                "maximum_x": 50,
                                "maximum_y": 50,
                            },
                            "variable_inventory": {
                                "variables": [
                                    {
                                        "name": "dem",
                                        "dtype": "float32",
                                        "dimensions": ["y", "x"],
                                        "shape": [5, 5],
                                        "member_scope": "shared",
                                    }
                                ]
                            },
                            "coordinate_summaries": [
                                {
                                    "name": "x",
                                    "axis": "x",
                                    "dtype": "float64",
                                    "size": 5,
                                    "first": 5,
                                    "last": 45,
                                    "resolution": 10,
                                    "unit": "m",
                                }
                            ],
                            "time_coverage": [
                                {
                                    "coordinate_name": "rain_time",
                                    "start": 0,
                                    "end": 600,
                                    "unit": "s",
                                }
                            ],
                            "supported_metadata": {"zarr_format": 3, "temporal_type": "relative"},
                            "repository_locator": "private repository must not be displayed",
                            "member_mapping": {
                                "members": [
                                    {"index": index, "label": label}
                                    for index, label in enumerate(
                                        self.server.state.member_labels.get(input_id, ["member-0"])
                                    )
                                ]
                            },
                        }
                        if confirmed and self.server.state.validation_state in (None, "accepted")
                        else None
                    ),
                },
            )
            return
        if path.startswith("/execution-api/v1/ensembles/") and path.endswith("/simulations"):
            if not self._require_token():
                return
            self.server.state.status_gets.append((path, self.headers.get("X-Session-Token", "")))
            ensemble_id = path.removeprefix("/execution-api/v1/ensembles/").removesuffix(
                "/simulations"
            )
            self._send_json(200, self.server.state.ensemble_simulations.get(ensemble_id, []))
            return
        if path == "/_allauth/app/v1/auth/session":
            token = self.headers.get("X-Session-Token")
            if token in self.server.state.email_by_token:
                self._send_json(200, {"meta": {"is_authenticated": True}})
            else:
                self._send_json(401, {"meta": {"is_authenticated": False}})
            return

        if path.startswith("/downloads/"):
            state = self.server.state
            state.download_headers[path] = {
                key.lower(): value for key, value in self.headers.items()
            }
            if state.download_denials > 0:
                state.download_denials -= 1
                self._send_json(403, {"detail": "Signed URL expired"})
                return
            if (
                self.headers.get("x-download-token") != "signed-only"
                or "X-Session-Token" in self.headers
            ):
                self._send_json(403, {"detail": "Invalid signed headers"})
                return
            if state.interrupt_download:
                self.send_response(200)
                self.send_header("Content-Length", str(len(state.package_archive)))
                self.end_headers()
                self.wfile.write(state.package_archive[: 1024 * 1024 + 17])
                return
            self._send_bytes(
                state.download_status,
                state.package_archive,
                content_type="application/octet-stream",
            )
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
    metadata_storage: ModuleType
    pull: ModuleType
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

    monkeypatch.setattr(ctx.pull, "PACKAGE_POLL_SECONDS", 0)
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
    ctx.metadata_storage.save_ensemble_draft(
        ctx.metadata_storage.EnsembleDraft(
            email="user@example.com",
            project_id="project-id",
            member_labels=("member-0",),
            grass_params=ctx.grass_params,
            archive_sha256="a" * 64,
            yaml_sha256="b" * 64,
            idempotency_key="status-key",
            ensemble_id="ensemble-1",
            input_id="input-1",
            run_ids={1: "run-1", 0: "run-0"},
        )
    )
    itzi_cloud_status(argparse.Namespace(ensemble_id=None))
    itzi_cloud_status(argparse.Namespace(ensemble_id="ensemble-1"))

    assert any("Ensemble: ensemble-1" in message for message in status_messages)
    assert any("Input: input-1; state: draft" in message for message in status_messages)
    assert not any("completed" in message or "PROGRESS" in message for message in status_messages)

    destination = tmp_path / "verified.archive"
    itzi_cloud_pull(argparse.Namespace(ensemble_id="ensemble-1", run_id=[], output=destination))
    state = fake_cloud_server.state
    assert destination.read_bytes() == state.package_archive
    assert state.package_requests == [
        (
            "/execution-api/v1/ensembles/ensemble-1/packages",
            {"run_ids": ["run-0", "run-1"]},
            "token-1",
        )
    ]
    assert (
        state.package_gets
        == [("/execution-api/v1/ensembles/ensemble-1/packages/package-1", "token-1")] * 3
    )
    assert len(state.download_headers) == 1
    headers = next(iter(state.download_headers.values()))
    assert headers["x-download-token"] == "signed-only"
    assert "x-session-token" not in headers
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "appdata",
        "grassdb",
        "verified.archive",
    ]


@pytest.mark.cloud
@pytest.mark.parametrize("input_state", ["uploading", "validating", "accepted", "failed"])
def test_cloud_status_reports_input_facts_and_recorded_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
    input_state: str,
) -> None:
    from itzi.cloud import status
    from itzi.cloud.metadata_storage import EnsembleDraft

    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    itzi_cloud_login(
        argparse.Namespace(email="user@example.com", password="secret", logout=False, status=False)
    )
    draft = EnsembleDraft(
        email="user@example.com",
        project_id="project-id",
        member_labels=("rain-a", "rain-b"),
        grass_params=ctx.grass_params,
        archive_sha256="a" * 64,
        yaml_sha256="b" * 64,
        idempotency_key="status-key",
        ensemble_id="ensemble-1",
        input_id="input-1",
        upload_stage="confirmed" if input_state != "uploading" else "uploaded",
        simulation_ids={0: "simulation-0"} if input_state == "accepted" else {},
        run_ids={0: "run-0"} if input_state == "accepted" else {},
    )
    ctx.metadata_storage.save_ensemble_draft(draft)
    state = fake_cloud_server.state
    state.member_labels["input-1"] = list(draft.member_labels)
    state.validation_state = input_state
    if input_state != "uploading":
        state.confirmed["input-1"] = {"size_bytes": 42, "sha256": "a" * 64}
    if input_state == "accepted":
        state.ensemble_simulations["ensemble-1"] = [
            {
                "ensemble_id": "ensemble-1",
                "input_id": "input-1",
                "simulation_id": "simulation-0",
                "member_index": 0,
                "member_label": "rain-a",
            }
        ]
    messages: list[str] = []
    monkeypatch.setattr(status.msgr, "message", messages.append)
    before = ctx.metadata_storage.get_metadata_file_path().read_bytes()

    itzi_cloud_status(argparse.Namespace(ensemble_id="ensemble-1"))

    assert state.status_gets == [
        ("/execution-api/v1/inputs/input-1", "token-1"),
        ("/execution-api/v1/ensembles/ensemble-1/simulations", "token-1"),
    ]
    assert ctx.metadata_storage.get_metadata_file_path().read_bytes() == before
    text = "\n".join(messages)
    assert f"Input: input-1; state: {input_state}" in text
    assert "Input storage (bytes):" in text
    assert "Account storage (bytes): entitlement=1000" in text
    assert "RECORDED RUN ID" in text
    assert "rain-a" in text and "rain-b" in text
    assert "execution state is unavailable" in text
    assert "PROGRESS" not in text and "completed" not in text
    assert "private" not in text
    assert ("Confirmed upload: 42 bytes" in text) == (input_state != "uploading")
    assert ("Acceptance: precipient-itzi-input-v1" in text) == (input_state == "accepted")
    if input_state == "accepted":
        assert "CRS WKT:" in text and "Cell-edge extent:" in text
        assert "Variable: dem" in text and "Coordinate: x" in text
        assert "Time coverage: rain_time" in text and "temporal type: relative" in text
        assert any(
            "rain-a" in message and "simulation-0" in message and "run-0" in message
            for message in messages
        )
        assert any("rain-b" in message and message.endswith("-") for message in messages)
    if input_state == "failed":
        assert "Failure: validation/invalid_archive: Archive rejected" in text
        assert "Next action: fix the archive" in text
    if input_state == "uploading":
        assert "Local upload stage: uploaded" in text


@pytest.mark.cloud
def test_cloud_pull_surfaces_api_detail_when_results_are_unavailable(
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
    fake_cloud_server.state.package_error = (
        409,
        {
            "detail": "Results are not available yet",
            "code": "run_not_exportable",
            "next_action": "select_exportable_runs",
        },
    )

    with pytest.raises(
        FatalError, match="Results are not available yet; next action: select_exportable_runs"
    ):
        itzi_cloud_pull(
            argparse.Namespace(
                ensemble_id="ensemble-1",
                run_id=["run-0"],
                output=tmp_path / "results.archive",
            )
        )


@pytest.mark.cloud
@pytest.mark.parametrize("refresh", ["expired_url", "denied_download"])
def test_cloud_pull_refreshes_signed_url_for_same_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
    refresh: str,
) -> None:
    _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    itzi_cloud_login(
        argparse.Namespace(email="user@example.com", password="secret", logout=False, status=False)
    )
    state = fake_cloud_server.state
    state.package_states = ["ready"]
    state.expired_package_urls = int(refresh == "expired_url")
    state.download_denials = int(refresh == "denied_download")
    destination = tmp_path / "result.archive"

    itzi_cloud_pull(
        argparse.Namespace(
            ensemble_id="ensemble-unknown", run_id=["run-2", "run-0"], output=destination
        )
    )

    assert destination.read_bytes() == state.package_archive
    assert state.package_requests == [
        (
            "/execution-api/v1/ensembles/ensemble-unknown/packages",
            {"run_ids": ["run-2", "run-0"]},
            "token-1",
        )
    ]
    assert len(state.package_gets) == 2
    assert len(state.download_headers) == (1 if refresh == "expired_url" else 2)
    assert all(
        headers["x-download-token"] == "signed-only" and "x-session-token" not in headers
        for headers in state.download_headers.values()
    )


@pytest.mark.cloud
@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("failed", "package_export_failed"),
        ("queued", "still queued"),
        ("running", "still running"),
        ("package_artifact_expired", "artifact is unavailable or expired"),
        ("missing_package_download", "artifact is unavailable or expired"),
        ("expired_package_urls", "repeatedly expired or was denied"),
        ("download_denials", "repeatedly expired or was denied"),
        ("wrong_package_id", "does not match the requested package manifest"),
        ("wrong_manifest", "does not match the requested package manifest"),
        ("too_short", "size mismatch"),
        ("too_long", "size mismatch"),
        ("wrong_package_digest", "SHA-256 mismatch"),
        ("not_found", "artifact is unavailable or expired"),
        ("gone", "artifact is unavailable or expired"),
        ("storage_failure", "Signed package download failed"),
        ("package_status_error", "verify_resource_access"),
        ("interrupt_download", "IncompleteRead"),
    ],
)
def test_cloud_pull_failures_never_publish_unverified_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
    failure: str,
    message: str,
) -> None:
    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    itzi_cloud_login(
        argparse.Namespace(email="user@example.com", password="secret", logout=False, status=False)
    )
    monkeypatch.setattr(ctx.pull, "PACKAGE_WAIT_SECONDS", 0)
    state = fake_cloud_server.state
    state.package_states = [failure if failure in ("failed", "queued", "running") else "ready"]
    if failure in ("expired_package_urls", "download_denials"):
        setattr(state, failure, 3)
    elif failure in ("too_short", "too_long"):
        state.package_size_delta = 1 if failure == "too_short" else -1
    elif failure in ("not_found", "gone", "storage_failure"):
        state.download_status = {"not_found": 404, "gone": 410, "storage_failure": 503}[failure]
    elif failure == "package_status_error":
        state.package_status_error = (
            404,
            {
                "detail": "Package not found",
                "code": "package_not_found",
                "next_action": "verify_resource_access",
            },
        )
    elif failure not in ("failed", "queued", "running"):
        setattr(state, failure, True)
    destination = tmp_path / "result.archive"

    with pytest.raises(FatalError, match=message):
        itzi_cloud_pull(
            argparse.Namespace(ensemble_id="ensemble-1", run_id=["run-0"], output=destination)
        )

    assert not destination.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["appdata", "grassdb"]
    assert len(state.package_requests) == 1


@pytest.mark.cloud
def test_cloud_pull_preserves_existing_destination(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
) -> None:
    _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    itzi_cloud_login(
        argparse.Namespace(email="user@example.com", password="secret", logout=False, status=False)
    )
    destination = tmp_path / "result.archive"
    destination.write_bytes(b"existing user file")

    with pytest.raises(FatalError, match="Destination already exists"):
        itzi_cloud_pull(
            argparse.Namespace(ensemble_id="ensemble-1", run_id=["run-0"], output=destination)
        )

    assert destination.read_bytes() == b"existing user file"
    assert fake_cloud_server.state.package_requests == []


@pytest.mark.cloud
@pytest.mark.parametrize("selection", ["missing", "other_account", "too_many", "maximum"])
def test_cloud_pull_checks_selection_before_requesting_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_cloud_server: FakeCloudServer,
    selection: str,
) -> None:
    ctx = _configure_cloud_test_environment(monkeypatch, tmp_path, fake_cloud_server)
    itzi_cloud_login(
        argparse.Namespace(email="user@example.com", password="secret", logout=False, status=False)
    )
    ctx.metadata_storage.save_ensemble_draft(
        ctx.metadata_storage.EnsembleDraft(
            email="other@example.com",
            project_id="project-id",
            member_labels=("member-0",),
            grass_params=ctx.grass_params,
            archive_sha256="a" * 64,
            yaml_sha256="b" * 64,
            idempotency_key="other-key",
            ensemble_id="other-ensemble",
            run_ids={0: "other-run"},
        )
    )
    runs = [f"run-{index}" for index in range(101 if selection == "too_many" else 100)]
    if selection in ("missing", "other_account"):
        runs = []
    state = fake_cloud_server.state
    state.package_states = ["ready"]
    destination = tmp_path / "result.archive"
    args = argparse.Namespace(
        ensemble_id="other-ensemble" if selection == "other_account" else "unknown-ensemble",
        run_id=runs,
        output=destination,
    )
    if selection == "maximum":
        itzi_cloud_pull(args)
        assert state.package_requests[0][1] == {"run_ids": runs}
        assert destination.read_bytes() == state.package_archive
    else:
        with pytest.raises(FatalError):
            itzi_cloud_pull(args)
        assert not destination.exists()
        assert state.package_requests == []


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
        itzi_cloud_status(argparse.Namespace(ensemble_id=None))


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
    description: str | None = None,
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
                "ensemble": {"id": f"study-{document_index}", "description": description},
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
            ensemble_id=ensemble.ensemble_id,
            ensemble_description=ensemble.ensemble_description,
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
            description="Flood study" if index == 1 else None,
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
    assert all(call[2] == "token-1" for call in fake_cloud_server.state.ensemble_creates)
    assert [json.loads(call[3]) for call in fake_cloud_server.state.ensemble_creates] == [
        {"name": "study-0", "description": ""},
        {"name": "study-1", "description": "Flood study"},
    ]
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
    assert [json.loads(call[3]) for call in state.ensemble_creates] == [
        {"name": "study-0", "description": ""}
    ] * 2
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
        {"ground_elevation": "raster_dem_0_in_mapset", "friction": "raster_n_in_mapset"},
        {"ground_elevation": "raster_dem_1_in_mapset", "friction": "raster_n_in_mapset"},
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
