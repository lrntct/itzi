from __future__ import annotations

import io
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from itzi.cloud import result_import
from itzi.cloud.metadata_storage import EnsembleDraft, MemberOutputContext
from itzi.grass.session import GrassParams
from tests.package_factory import result_dataset, write_package


@pytest.mark.parametrize("relative", [False, True])
def test_package_recovers_native_times_and_available_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
) -> None:
    path = write_package(tmp_path, result_dataset(relative=relative))
    monkeypatch.setattr(result_import, "list_ensemble_drafts", lambda _email: ())
    captured = []

    def capture(ds, crs, members, times, temporal_type, destination, overwrite):
        captured.append((members, times, temporal_type, destination))
        np.testing.assert_allclose(ds.water_depth.isel(member=0, time=1).values[1, 1], 0.01)

    monkeypatch.setattr(result_import, "_write_results", capture)
    target = GrassParams(grassdata=str(tmp_path), location="location", mapset="mapset")
    result_import.import_result_package(path, destination=target)
    members, times, temporal_type, destination = captured[0]
    assert [member.count for member in members] == [3, 2]
    assert [member.hmin for member in members] == [0, 0]
    assert members[0].output_names != members[1].output_names
    assert times == (
        (timedelta(), timedelta(minutes=30), timedelta(hours=1))
        if relative
        else tuple(
            datetime.fromisoformat(value)
            for value in ("2007-06-25T10:00:00", "2007-06-25T10:30:00", "2007-06-25T11:00:00")
        )
    )
    assert temporal_type == ("relative" if relative else "absolute")
    assert destination == target


def test_saved_context_is_matched_by_run_not_member_position() -> None:
    ds = result_dataset()
    package, _ = result_import.validate_dataset(ds)
    draft = EnsembleDraft(
        email="email",
        project_id="project",
        member_labels=("b", "a"),
        grass_params=GrassParams(),
        archive_sha256="digest",
        idempotency_key="key",
        yaml_sha256="yaml",
        ensemble_id="ensemble",
        input_id="input",
        run_ids={0: "run-b", 1: "run-a"},
        simulation_ids={0: "sim-b", 1: "sim-a"},
        member_outputs={
            0: MemberOutputContext(output_map_names={"water_depth": "original_b"}, hmin=0.2),
            1: MemberOutputContext(output_map_names={"water_depth": "original_a"}, hmin=0.1),
        },
    )
    members = result_import.plan_members(ds, package, (draft,), None, None)
    assert members[0].output_names[0] == ("water_depth", "original_a")
    assert members[1].output_names[0] == ("water_depth", "original_b")
    assert [member.hmin for member in members] == [0.1, 0.2]
    overridden = result_import.plan_members(ds, package, (draft,), "chosen", 0.3)
    assert overridden[0].output_names[0][1].startswith("chosen_")
    assert overridden[0].hmin == 0.3


@pytest.mark.parametrize("bad", ["flags", "grid", "runs", "variables"])
def test_invalid_package_contract_rejected(bad: str) -> None:
    ds = result_dataset()
    if bad == "flags":
        ds.output_available.values[0, 1] = 0
    elif bad == "grid":
        ds = ds.assign_coords(y=[15, 25, 35])
    elif bad == "runs":
        ds.run_id.values[1] = "run-a"
    else:
        ds = ds.drop_vars("water_depth")
    with pytest.raises(ValueError):
        result_import.validate_dataset(ds)


@pytest.mark.parametrize("kind", ["escape", "symlink", "duplicate"])
def test_extraction_rejects_unsafe_or_duplicate_entries(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "bad.tzst"
    with tarfile.open(path, "w:zst") as archive:
        info = tarfile.TarInfo("../outside" if kind == "escape" else "results.zarr/zarr.json")
        info.size = 2
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "../outside"
        archive.addfile(info, io.BytesIO(b"{}"))
        if kind == "duplicate":
            archive.addfile(info, io.BytesIO(b"{}"))
    with pytest.raises(ValueError, match="Invalid result package entry"):
        result_import.extract_package(path, tmp_path / "extracted")
    assert not (tmp_path / "outside").exists()


def test_time_validation_rejects_duplicate_records() -> None:
    time = xr.DataArray(np.array([0, 0], dtype="timedelta64[s]"), dims="time")
    with pytest.raises(ValueError, match="increasing"):
        result_import.native_record_times(time)
