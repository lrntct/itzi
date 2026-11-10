from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import grass.script as gscript
import numpy as np
import pytest
from itzi_core import DomainData, TemporalType
from pyproj import CRS

from itzi.cloud import result_import
from itzi.cloud.metadata_storage import EnsembleDraft, MemberOutputContext
from itzi.grass.session import GrassParams, GrassSessionManager
from tests.package_factory import LOCAL_CRS_WKT, result_dataset, write_package


@pytest.mark.usefixtures("grass_5by5")
@pytest.mark.parametrize("relative", [False, True])
def test_import_matches_native_outputs_and_restores_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
) -> None:
    import grass.temporal as tgis
    from grass.pygrass.raster import RasterRow

    from itzi.grass.interface import GrassInterface
    from itzi.providers.grass_output import GrassRasterOutputProvider

    ds = result_dataset(relative=relative)
    path = write_package(tmp_path, ds)
    grass_env = gscript.gisenv()
    target = GrassParams(
        grassdata=grass_env["GISDBASE"],
        location=grass_env["LOCATION_NAME"],
        mapset=grass_env["MAPSET"],
    )
    draft = EnsembleDraft(
        email="email",
        project_id="project",
        member_labels=("b", "a"),
        grass_params=target,
        archive_sha256="digest",
        idempotency_key="key",
        yaml_sha256="yaml",
        ensemble_id="ensemble",
        input_id="input",
        run_ids={0: "run-b", 1: "run-a"},
        simulation_ids={0: "sim-b", 1: "sim-a"},
        member_outputs={
            i: MemberOutputContext(
                output_map_names={
                    code: f"imported_{label}_{code}" for code in ("water_depth", "max_water_depth")
                },
                hmin=0.1,
            )
            for i, label in enumerate(("b", "a"))
        },
    )
    monkeypatch.setattr(result_import, "list_ensemble_drafts", lambda _email: (draft,))
    gscript.run_command("g.region", n=1, s=0, e=1, w=0, rows=1, cols=1)
    original_region = gscript.region()
    original_overwrite = gscript.overwrite()
    domain = DomainData(north=40, south=10, east=50, west=0, rows=3, cols=5, crs_wkt=LOCAL_CRS_WKT)
    with GrassSessionManager(GrassParams(), domain=domain):
        interface = GrassInterface(
            datetime(2007, 6, 25, 10), datetime(2007, 6, 25, 11), np.float32
        )
        native = GrassRasterOutputProvider(
            interface,
            {code: f"native_{code}@5by5" for code in ("water_depth", "max_water_depth")},
            0.1,
            TemporalType.RELATIVE if relative else TemporalType.ABSOLUTE,
        )
        for record in range(3):
            time = timedelta(minutes=30 * record)
            if not relative:
                time = datetime(2007, 6, 25, 10) + time
            native.write_arrays(
                {
                    code: ds[code].isel(member=0, time=record).values
                    for code in native.out_map_names
                },
                time,
            )
        native.finalize()
    result_import.import_result_package(path)
    assert gscript.region() == original_region
    assert gscript.overwrite() == original_overwrite
    colors = (
        Path(grass_env["GISDBASE"]) / grass_env["LOCATION_NAME"] / grass_env["MAPSET"] / "colr"
    )
    for code in ("water_depth", "max_water_depth"):
        native_stds = tgis.open_old_stds(f"native_{code}@5by5", "strds")
        imported = tgis.open_old_stds(f"imported_a_{code}@5by5", "strds")
        expected_maps = native_stds.get_registered_maps_as_objects(order="start_time")
        actual_maps = imported.get_registered_maps_as_objects(order="start_time")
        assert [m.get_temporal_extent_as_tuple() for m in actual_maps] == [
            m.get_temporal_extent_as_tuple() for m in expected_maps
        ]
        assert all(m.get_temporal_extent_as_tuple()[1] is None for m in actual_maps)
        for record in (1, 2):
            expected = f"native_{code}_{record:04d}"
            actual = f"imported_a_{code}_{record:04d}"
            assert (colors / actual).read_bytes() == (colors / expected).read_bytes()
            with (
                GrassSessionManager(GrassParams(), domain=domain),
                RasterRow(expected) as reference,
                RasterRow(actual) as output,
            ):
                assert (output.info.rows, output.info.cols) == (3, 5)
                values = ds[code].isel(member=0, time=record).values.copy()
                if code == "water_depth":
                    values[(values >= 0) & (values <= 0.1)] = np.nan
                for row in range(3):
                    np.testing.assert_array_equal(np.asarray(output[row]), values[row])
                    np.testing.assert_array_equal(np.asarray(reference[row]), values[row])
        partial = tgis.open_old_stds(f"imported_b_{code}@5by5", "strds")
        assert len(partial.get_registered_maps_as_objects()) == len(actual_maps) - 1
    with pytest.raises(RuntimeError, match="exists and will not be overwritten"):
        result_import.import_result_package(path)
    assert gscript.region() == original_region
    result_import.import_result_package(path, overwrite=True)
    assert gscript.region() == original_region


@pytest.mark.usefixtures("grass_5by5")
def test_import_into_fresh_project_without_push_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GRASS_VERBOSE", raising=False)
    ds = result_dataset()
    for code in ("water_depth", "max_water_depth"):
        ds[code].attrs["proj:wkt2"] = CRS.from_epsg(27700).to_wkt()
    path = write_package(tmp_path, ds)
    gscript.create_project(tmp_path, name="fresh", epsg=27700)
    target = GrassParams(grassdata=str(tmp_path), location="fresh", mapset="PERMANENT")
    original_context = GrassSessionManager.current_context()
    original_region = gscript.region()
    monkeypatch.setattr(result_import, "list_ensemble_drafts", lambda _email: ())
    result_import.import_result_package(path, destination=target, prefix="copied")
    assert GrassSessionManager.current_context() == original_context
    assert gscript.region() == original_region
    assert list((tmp_path / "fresh" / "PERMANENT" / "cellhd").glob("copied_*_water_depth_0000"))
    assert "GRASS_VERBOSE" not in os.environ
