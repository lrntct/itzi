"""Smoke-test cloud packaging against the current GRASS interfaces."""

import tarfile
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from multiprocessing import get_context
from pathlib import Path

import grass.script as gscript
import numpy as np
import pytest
from pyproj import CRS

from itzi.cloud import archive
from itzi.ensemble import load_yaml_stream
from itzi.ensemble.models import EffectiveMask, OutputTemplates, ResolvedSimulation
from itzi.ensemble.resolution import resolve_ensemble
from itzi.grass.session import GrassSessionManager


@pytest.mark.forked
def test_yaml_cloud_archive_from_grass(grass_5by5, tmp_path: Path, caplog) -> None:
    GrassSessionManager.ensure_temporal_initialized()
    gscript.mapcalc("cloud_rain0=1")
    gscript.mapcalc("cloud_rain1=2")
    gscript.mapcalc("cloud_rain2=3")
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
    with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as executor:
        results = executor.submit(resolve_ensemble, cloud_members).result()
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
    import xarray as xr
    import zarr

    capability = archive.InputFormat(
        limits=limits,
        supported_versions=archive.SupportedVersions(
            xarray=[xr.__version__],
            zarr_python=[zarr.__version__],
            accepted_extensions=[archive.WriterExtension(name="fixed_length_utf32", version="1")],
        ),
    )
    built = archive.build_archive(ensemble, simulations, capability, config.parent / "smoke.tzst")
    assert built.path.is_file()
    assert "statistics CSV output will not run" not in caplog.text
    assert "SWMM coupling/drainage will not run" in caplog.text

    gscript.mapcalc("cloud_explicit_mask=if(col() == 1,null(),1)")
    gscript.mapcalc("cloud_active_mask=if(col() == 5,null(),1)")
    gscript.run_command("r.mask", raster="cloud_active_mask")
    masked = tuple(
        replace(
            simulation,
            grass_params=replace(simulation.grass_params, mask="cloud_explicit_mask@5by5"),
            effective_mask=EffectiveMask("explicit", "cloud_explicit_mask@5by5"),
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
    roles = list(simulations[0].simulation_config.input_map_names)
    assert len(ds.data_vars) < len(simulations) * len(roles)
    ground = ds[f"source_{roles.index('ground_elevation')}"].values
    assert np.isnan(ground[:, 0]).all()
    assert np.isfinite(ground[:, -1]).all()  # Active MASK is not the explicit mask.
    rainfall = ds[f"source_{roles.index('rainfall_rate')}"].values
    assert rainfall.shape == (3, 5, 5)
    assert np.isnan(rainfall[:, :, 0]).all()
    assert np.isfinite(rainfall[:, :, -1]).all()
