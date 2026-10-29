"""Regression checks for local cloud metadata storage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from itzi.cloud import metadata_storage
from itzi.grass.session import GrassParams


@pytest.mark.parametrize("invalid_json", [False, True])
def test_save_simulation_does_not_replace_corrupted_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, invalid_json: bool
) -> None:
    monkeypatch.setattr(metadata_storage, "user_data_dir", lambda *_: str(tmp_path))
    metadata_storage.save_ensemble_draft(
        metadata_storage.EnsembleDraft(
            email="user@example.com",
            project_id="project",
            member_labels=("member",),
            grass_params=GrassParams(),
            archive_sha256="hash",
            idempotency_key="draft-key",
            yaml_sha256="yaml-hash",
        )
    )
    path = metadata_storage.get_metadata_file_path()
    if invalid_json:
        path.write_text("{invalid json")
    else:
        contents = json.loads(path.read_text())
        contents["simulations"]["existing"] = {"grass_params": "invalid"}
        path.write_text(json.dumps(contents))
    before = path.read_bytes()

    with pytest.raises(ValueError):
        metadata_storage.save_simulation_metadata(
            "new", "user@example.com", "study.yaml", GrassParams()
        )

    assert path.read_bytes() == before


def test_save_simulation_preserves_existing_metadata_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(metadata_storage, "user_data_dir", lambda *_: str(tmp_path))
    metadata_storage.save_simulation_metadata("old", "user@example.com", "old.yaml", GrassParams())
    path = metadata_storage.get_metadata_file_path()
    contents = json.loads(path.read_text())
    contents["extension"] = {"preserve": True}
    contents["simulations"]["old"]["note"] = "keep"
    contents["simulations"]["old"]["grass_params"]["region"] = "keep"
    path.write_text(json.dumps(contents))

    metadata_storage.save_simulation_metadata("new", "user@example.com", "new.yaml", GrassParams())

    stored = json.loads(path.read_text())
    assert stored["extension"] == {"preserve": True}
    assert stored["simulations"]["old"]["note"] == "keep"
    assert stored["simulations"]["old"]["grass_params"]["region"] == "keep"
    assert set(stored["simulations"]) == {"old", "new"}


def test_reset_ensemble_preserves_unrelated_metadata_and_resumes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(metadata_storage, "user_data_dir", lambda *_: str(tmp_path))
    draft = metadata_storage.get_or_create_ensemble_draft(
        "user@example.com", "project", ("member",), GrassParams(), "archive-hash", "yaml-hash"
    )
    unrelated = draft.model_copy(
        update={"email": "other@example.com", "idempotency_key": "other-key"}
    )
    metadata_storage.save_ensemble_draft(unrelated)
    path = metadata_storage.get_metadata_file_path()
    contents = json.loads(path.read_text())
    contents["extension"] = {"preserve": True}
    contents["ensembles"][draft.idempotency_key]["note"] = "keep"
    path.write_text(json.dumps(contents))
    draft = metadata_storage.list_ensemble_drafts(draft.email)[0]

    reset = metadata_storage.reset_ensemble_draft(draft)

    stored = json.loads(path.read_text())
    assert stored["extension"] == contents["extension"]
    assert (
        stored["ensembles"][unrelated.idempotency_key]
        == contents["ensembles"][unrelated.idempotency_key]
    )
    assert stored["ensembles"][reset.idempotency_key]["note"] == "keep"
    assert draft.idempotency_key not in stored["ensembles"]
    assert (
        metadata_storage.get_or_create_ensemble_draft(
            draft.email,
            draft.project_id,
            draft.member_labels,
            draft.grass_params,
            draft.archive_sha256,
            draft.yaml_sha256,
        )
        == reset
    )
