"""Provider-free checks for local submission selection and honest status reporting."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests

from itzi.cloud import metadata_storage, status
from itzi.cloud.metadata_storage import EnsembleDraft
from itzi.cloud.schemas import (
    AcceptedMemberSchema,
    InputAcceptanceSchema,
    InputDetailSchema,
    SimulationResponseSchema,
)
from itzi.grass.session import GrassParams


def _draft(key: str, email: str = "user@example.com", *, created: bool = True) -> EnsembleDraft:
    return EnsembleDraft(
        email=email,
        project_id="project-1",
        member_labels=("rain-a", "rain-b"),
        grass_params=GrassParams(),
        archive_sha256="a" * 64,
        idempotency_key=key,
        yaml_sha256="b" * 64,
        ensemble_id=f"ensemble-{key}" if created else None,
        input_id=f"input-{key}" if created else None,
        run_ids={0: "run-0"},
    )


def _input_detail(draft: EnsembleDraft) -> dict[str, str | None | dict[str, int]]:
    return {
        "project_id": draft.project_id,
        "ensemble_id": draft.ensemble_id,
        "input_id": draft.input_id,
        "state": "uploading",
        "state_changed_at": "2026-10-01T12:00:00+00:00",
        "upload_confirmation": None,
        "acceptance": None,
        "input_storage": {"reserved_bytes": 42, "accepted_bytes": 0},
        "account_storage": {
            "reserved_bytes": 42,
            "accepted_bytes": 0,
            "available_bytes": 958,
            "entitlement_bytes": 1000,
        },
    }


@pytest.mark.parametrize("ensemble_id", [None, "ensemble-one"])
def test_status_selects_local_account_and_uses_execution_endpoints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ensemble_id: str | None
) -> None:
    monkeypatch.setattr(metadata_storage, "user_data_dir", lambda *_: str(tmp_path))
    monkeypatch.setenv("ITZI_CLOUD_API_BASE", "https://cloud.example.test/")
    one, two = _draft("one"), _draft("two")
    for draft in (one, two, _draft("other", "other@example.com")):
        metadata_storage.save_ensemble_draft(draft)
    requests_made: list[str] = []

    def get(url: str, headers: dict[str, str], timeout: int) -> requests.Response:
        assert headers == {"X-Session-Token": "session-token"}
        assert timeout == 30
        requests_made.append(url)
        response = requests.Response()
        response.status_code = 200
        payload = []
        for draft in (one, two):
            if url.endswith(f"/inputs/{draft.input_id}"):
                payload = _input_detail(draft)
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(status.requests, "get", get)
    results = status.get_ensemble_statuses("session-token", "user@example.com", ensemble_id)
    expected = (one,) if ensemble_id else (one, two)
    assert tuple(result.draft for result in results) == expected
    assert requests_made == [
        url
        for draft in expected
        for url in (
            f"https://cloud.example.test/execution-api/v1/inputs/{draft.input_id}",
            f"https://cloud.example.test/execution-api/v1/ensembles/{draft.ensemble_id}/simulations",
        )
    ]
    with pytest.raises(ValueError, match="No local submission found"):
        status.get_ensemble_statuses("session-token", "user@example.com", "ensemble-other")


def test_status_reports_empty_and_uncreated_submissions_without_remote_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(metadata_storage, "user_data_dir", lambda *_: str(tmp_path))
    monkeypatch.setattr(
        status.requests, "get", lambda *args, **kwargs: pytest.fail("unexpected GET")
    )
    messages: list[str] = []
    monkeypatch.setattr(status.msgr, "message", messages.append)

    status.display_ensemble_statuses(status.get_ensemble_statuses("token", "user@example.com"))
    assert messages == ["No locally recorded cloud submissions found for this account."]
    messages.clear()
    status.display_ensemble_table(())
    assert messages == ["No locally recorded cloud submissions found for this account."]
    with pytest.raises(ValueError, match="No local submission found for Ensemble missing"):
        status.get_ensemble_statuses("token", "user@example.com", "missing")

    metadata_storage.save_ensemble_draft(_draft("pending", created=False))
    results = status.get_ensemble_statuses("token", "user@example.com")
    assert results[0].input is None
    status.display_ensemble_statuses(results)
    assert any("creation pending locally; retry cloud push" in message for message in messages)
    assert any("rain-b" in message for message in messages)


@pytest.mark.parametrize("resource", ["input", "simulations"])
def test_status_surfaces_http_errors(monkeypatch: pytest.MonkeyPatch, resource: str) -> None:
    draft = _draft("one")
    monkeypatch.setattr(status, "list_ensemble_drafts", lambda email: (draft,))

    def get(url: str, **kwargs: str | int | dict[str, str]) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(_input_detail(draft)).encode()
        if resource == "input" or url.endswith("/simulations"):
            response.status_code = 404
        return response

    monkeypatch.setattr(status.requests, "get", get)
    with pytest.raises(requests.HTTPError):
        status.get_ensemble_statuses("token", draft.email)


def test_status_rejects_mismatched_input_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    draft = _draft("one")
    monkeypatch.setattr(status, "list_ensemble_drafts", lambda email: (draft,))
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(_input_detail(draft) | {"ensemble_id": "wrong"}).encode()
    monkeypatch.setattr(status.requests, "get", lambda *args, **kwargs: response)

    with pytest.raises(ValueError, match="does not match recorded Ensemble"):
        status.get_ensemble_statuses("token", draft.email)


def test_status_uses_local_timezone_for_api_timestamps(monkeypatch: pytest.MonkeyPatch) -> None:
    draft = _draft("one")
    detail = InputDetailSchema.model_validate(_input_detail(draft))
    messages: list[str] = []
    monkeypatch.setattr(status.msgr, "message", messages.append)

    try:
        with monkeypatch.context() as context:
            context.setenv("TZ", "Europe/Paris")
            time.tzset()
            status.display_ensemble_statuses((status.EnsembleStatus(draft, detail, ()),))
    finally:
        time.tzset()

    assert any("changed: 2026-10-01 14:00:00+02:00" in message for message in messages)


@pytest.mark.parametrize(
    ("size_bytes", "formatted_size"),
    [(0, "0 B"), (999, "999 B"), (1000, "1.0 kB"), (1_234_567, "1.2 MB"), (10**9, "1.0 GB")],
)
def test_status_table_summarizes_inputs_with_decimal_storage_and_local_acceptance(
    monkeypatch: pytest.MonkeyPatch, size_bytes: int, formatted_size: str
) -> None:
    draft = _draft("one-with-a-long-ensemble-id")
    uploading = InputDetailSchema.model_validate(_input_detail(draft))
    uploading.input_storage.reserved_bytes = size_bytes
    acceptance = InputAcceptanceSchema.model_validate(
        {
            "contract_revision": "v1",
            "accepted_at": datetime(2026, 10, 1, 12, tzinfo=UTC),
            "variable_inventory": {"variables": ()},
            "crs": {"wkt": "local"},
            "extent": {"minimum_x": 0, "minimum_y": 0, "maximum_x": 1, "maximum_y": 1},
            "coordinate_summaries": [
                {
                    "name": axis,
                    "axis": axis,
                    "dtype": "float64",
                    "size": size,
                    "first": 0,
                    "last": size - 1,
                }
                for axis, size in (("x", 3), ("y", 5), ("member", 7))
            ],
            "time_coverage": (),
            "supported_metadata": {"temporal_type": "relative"},
        }
    )
    accepted = uploading.model_copy(
        update={
            "state": "accepted",
            "acceptance": acceptance,
            "members": [AcceptedMemberSchema(index=0, label="accepted-a")],
        }
    )
    accepted.input_storage = uploading.input_storage.model_copy(
        update={"reserved_bytes": 0, "accepted_bytes": size_bytes}
    )
    messages: list[str] = []
    monkeypatch.setattr(status.msgr, "message", messages.append)

    try:
        with monkeypatch.context() as context:
            context.setenv("TZ", "Europe/Paris")
            time.tzset()
            status.display_ensemble_table(
                (
                    status.EnsembleStatus(draft, uploading, ()),
                    status.EnsembleStatus(_draft("two"), accepted, ()),
                    status.EnsembleStatus(_draft("pending", created=False), None, ()),
                )
            )
    finally:
        time.tzset()

    assert len(messages) == 4
    assert messages[0].split() == [
        "ENSEMBLE_ID",
        "INPUT",
        "STATUS",
        "#2D_CELLS",
        "#MEMBERS",
        "STORAGE",
        "ACCEPTED",
        "AT",
    ]
    assert messages[1].startswith(draft.ensemble_id or "")
    assert "uploading" in messages[1] and formatted_size in messages[1]
    assert messages[1].endswith("-")
    assert "accepted" in messages[2] and formatted_size in messages[2]
    assert messages[2].endswith("2026-10-01 14:00:00+02:00")
    assert "not recorded" in messages[3] and "pending locally" in messages[3]
    assert messages[3].endswith("-")
    assert messages[0].index("INPUT STATUS") == messages[1].index("uploading")
    assert messages[0].index("STORAGE") == messages[2].index(formatted_size)
    counts_start = messages[0].index("#2D_CELLS")
    counts_end = messages[0].index("STORAGE")
    assert messages[1][counts_start:counts_end].split() == ["-", "2"]
    assert messages[2][counts_start:counts_end].split() == ["15", "1"]
    assert messages[3][counts_start:counts_end].split() == ["-", "2"]


def test_status_keeps_members_in_order_and_excludes_other_input_revisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = _draft("one")
    detail = InputDetailSchema.model_validate(_input_detail(draft)).model_copy(
        update={
            "members": [
                AcceptedMemberSchema(index=1, label="accepted-b"),
                AcceptedMemberSchema(index=0, label="accepted-a"),
            ]
        }
    )
    simulation = SimulationResponseSchema(
        ensemble_id="ensemble-one",
        input_id="another-input",
        simulation_id="another-simulation",
        member_index=0,
        member_label="another-label",
    )
    messages: list[str] = []
    monkeypatch.setattr(status.msgr, "message", messages.append)

    status.display_ensemble_statuses((status.EnsembleStatus(draft, detail, (simulation,)),))

    rows = [message for message in messages if "accepted-" in message]
    assert len(rows) == 2
    assert "accepted-a" in rows[0] and "run-0" in rows[0]
    assert "accepted-b" in rows[1] and rows[1].endswith("-")
    assert not any("another-" in message for message in messages)
