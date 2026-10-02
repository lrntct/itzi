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
