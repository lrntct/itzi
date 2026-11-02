"""Smoke-test cloud packaging against the current GRASS interfaces."""

import tarfile
from dataclasses import replace
from pathlib import Path
from typing import Literal

import grass.script as gscript
import numpy as np
import pytest
import xarray as xr
import zarr
from pyproj import CRS

from itzi.cloud import archive
from itzi.ensemble import load_yaml_stream
from itzi.ensemble.models import EffectiveMask, OutputTemplates, ResolvedSimulation
from itzi.ensemble.resolution import resolve_ensemble
from itzi.grass.session import GrassSessionManager


@pytest.mark.parametrize("mask_mode", ["active", "explicit"])
def test_yaml_cloud_archive_from_grass(
    grass_5by5: None,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    mask_mode: Literal["active", "explicit"],
) -> None:
    GrassSessionManager.ensure_temporal_initialized()
    gscript.mapcalc("z=if(row() == 3 && col() == 3, null(), 0)", overwrite=True)
    gscript.mapcalc("cloud_rain0=float(1)")
    gscript.mapcalc("cloud_rain1=float(2)")
    gscript.mapcalc("cloud_rain2=float(3)")
    gscript.run_command(
        "t.create",
        output="cloud_series",
        type="strds",
        temporaltype="relative",
        semantictype="mean",
        title="cloud_series",
        description="cloud_series",
    )
    gscript.run_command(
        "t.register",
        flags="i",
        input="cloud_series",
        type="raster",
        maps="cloud_rain0,cloud_rain1,cloud_rain2",
        start="0",
        increment="30",
        unit="seconds",
    )
    config = tmp_path / "cloud-input.yaml"
    config.write_text(
        """\
schema_version: 1
ensemble: {id: cloud-smoke}
grass: {}
time: {duration: '00:01:00', record_step: '00:00:30'}
input:
  ground_elevation: z
  friction: n
  water_depth: [start_h, z]
  rainfall_rate: cloud_series
parameters: {}
drainage: {swmm_input: missing.inp}
outputs:
  statistics: {file: cloud-input.yaml}
  drainage: {vector_dataset: cloud_drain}
""",
        encoding="utf-8",
    )
    ensemble = load_yaml_stream(config).ensembles[0]
    cloud_members = tuple(
        replace(member, drainage=None, outputs=OutputTemplates(None, (), None, None))
        for member in ensemble.simulations
    )
    results = resolve_ensemble(cloud_members)
    assert all(isinstance(result, ResolvedSimulation) for result in results), results
    simulations = tuple(result for result in results if isinstance(result, ResolvedSimulation))
    assert simulations[0].domain_data.crs_wkt.strip() == "XY location (unprojected)"
    limits = archive.InputLimits.model_validate(
        dict.fromkeys(archive.InputLimits.model_fields, 1_000_000_000)
        | {
            "INPUT_MAX_ZSTD_WINDOW_LOG": 23,
            "INPUT_MIN_SPATIAL_COORDINATE_SAMPLES": 5,
        }
    )
    capability = archive.InputFormat(limits=limits)
    built = archive.build_archive(ensemble, simulations, capability, config.parent / "smoke.tzst")
    assert built.path.is_file()
    assert "statistics CSV output will not run" not in caplog.text
    assert "SWMM coupling/drainage will not run" in caplog.text
    with tarfile.open(built.path, "r:zst") as tar:
        tar.extractall(config.parent / "smoke-extracted", filter="data")
    unmasked = xr.open_zarr(config.parent / "smoke-extracted" / "input.zarr", consolidated=False)
    assert unmasked.MASK.dims == ("y", "x")
    assert unmasked.MASK.dtype == np.dtype(bool)
    assert not unmasked.MASK.values.any()
    assert np.isnan(unmasked["raster_z_in_5by5"].values[2, 2])
    np.testing.assert_array_equal(
        unmasked["strds_cloud_series_in_5by5"].values[:, 0, 0], [1, 2, 3]
    )

    gscript.mapcalc("cloud_explicit_mask=if(col() == 1,null(),1)")
    gscript.mapcalc("cloud_active_mask=if(col() == 5,null(),1)")
    gscript.run_command("r.mask", raster="cloud_active_mask")
    masked = tuple(
        replace(
            simulation,
            grass_params=replace(
                simulation.grass_params,
                mask="cloud_explicit_mask@5by5" if mask_mode == "explicit" else None,
            ),
            effective_mask=EffectiveMask(
                mask_mode, "cloud_explicit_mask@5by5" if mask_mode == "explicit" else "MASK@5by5"
            ),
        )
        for simulation in simulations
    )
    masked_archive = archive.build_archive(
        ensemble, masked, capability, config.parent / "masked.tzst"
    )
    with tarfile.open(masked_archive.path, "r:zst") as tar:
        tar.extractall(config.parent / "masked-extracted", filter="data")
    ds = xr.open_zarr(config.parent / "masked-extracted" / "input.zarr", consolidated=False)
    assert CRS.from_wkt(ds.attrs["crs_wkt"]).is_engineering
    assert len(ds.data_vars) < len(simulations) * len(
        simulations[0].simulation_config.input_map_names
    )
    assert ds.MASK.dims == ("y", "x")
    assert ds.MASK.dtype == np.dtype(bool)
    expected_mask = np.zeros((5, 5), dtype=bool)
    excluded_column = 0 if mask_mode == "explicit" else -1
    included_column = -1 if mask_mode == "explicit" else 0
    expected_mask[:, excluded_column] = True
    np.testing.assert_array_equal(ds.MASK.values, expected_mask)
    assert ds.attrs["itzi_dimension_names"]["MASK"] == {"y": "y", "x": "x"}
    stored_mask = zarr.open_array(
        config.parent / "masked-extracted" / "input.zarr" / "MASK", mode="r"
    )
    assert stored_mask.dtype == np.dtype(bool)
    ground = ds["raster_z_in_5by5"].values
    assert np.isnan(ground[:, excluded_column]).all()
    assert np.isfinite(ground[:, included_column]).all()
    assert np.isnan(ground[2, 2])
    rainfall = ds["strds_cloud_series_in_5by5"].values
    assert rainfall.shape == (3, 5, 5)
    assert np.isnan(rainfall[:, :, excluded_column]).all()
    np.testing.assert_array_equal(rainfall[:, 0, included_column], [1, 2, 3])
    assert np.isfinite(rainfall[:, :, included_column]).all()
