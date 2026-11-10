"""Small API-shaped Zarr packages shared by reader and GRASS integration tests."""

from __future__ import annotations

import tarfile
from pathlib import Path

import numpy as np
import xarray as xr

LOCAL_CRS_WKT = (
    'ENGCRS["Local engineering CRS",EDATUM["Unknown engineering datum"],'
    'CS[Cartesian,2],AXIS["x",east,ORDER[1],LENGTHUNIT["metre",1]],'
    'AXIS["y",north,ORDER[2],LENGTHUNIT["metre",1]]]'
)


def result_dataset(*, relative: bool = False) -> xr.Dataset:
    """Include initialization, sub-threshold depths, NULLs, and a partial member."""
    values = np.arange(2 * 3 * 3 * 5, dtype=np.float32).reshape(2, 3, 3, 5) / 10
    values[:, 0] = 0
    values[:, :, 0, 0] = np.nan
    values[0, 1, 1, 1] = 0.01
    values[1, 2] = np.nan
    attrs = {
        "proj:wkt2": LOCAL_CRS_WKT,
        "spatial:dimensions": ["y", "x"],
        "spatial:transform": [10.0, 0.0, 0.0, 0.0, -10.0, 40.0],
        "spatial:registration": "pixel",
    }
    units = "microseconds" if relative else "microseconds since 2007-06-25 10:00:00 +01:00"
    ds = xr.Dataset(
        data_vars={
            "water_depth": (("member", "time", "y", "x"), values, attrs),
            "max_water_depth": (("member", "time", "y", "x"), values.copy(), attrs),
            "run_id": ("member", ["run-a", "run-b"]),
            "simulation_id": ("member", ["sim-a", "sim-b"]),
            "input_id": ("member", ["input", "input"]),
            "output_available": (("member", "time"), np.array([[1, 1, 1], [1, 1, 0]], dtype="u1")),
            "output_count": ("member", [3, 2]),
        },
        coords={
            "member": [0, 1],
            "member_label": ("member", ["member-a", "member-b"]),
            "time": ("time", np.array([0, 1800_000000, 3600_000000]), {"units": units}),
            "y": [35.0, 25.0, 15.0],
            "x": [5.0, 15.0, 25.0, 35.0, 45.0],
        },
        attrs={
            "precipient:package": {
                "schema_version": 1,
                "format": "precipient-zarr-tzst-v1",
                "project_id": "project",
                "ensemble_id": "ensemble",
                "output_codes": ["water_depth", "max_water_depth"],
            }
        },
    )
    for name in ("member_label", "run_id", "simulation_id", "input_id"):
        ds[name] = ds[name].astype("T")
    return ds


def write_package(directory: Path, ds: xr.Dataset) -> Path:
    store = directory / "results.zarr"
    ds.to_zarr(store, zarr_format=3, consolidated=False)
    path = directory / "results.tzst"
    with tarfile.open(path, "w:zst") as archive:
        archive.add(store, arcname="results.zarr")
    return path
