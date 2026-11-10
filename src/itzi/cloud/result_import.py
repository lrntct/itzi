"""Read self-describing result packages and replay the native GRASS output path."""

from __future__ import annotations

import hashlib
import math
import os
import re
import tarfile
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Literal

import numpy as np
import pandas as pd
import xarray as xr
from itzi_core import OUTPUT_ARRAY_KEYS, DomainData, TemporalType
from pydantic import BaseModel, Field
from pyproj import CRS

import itzi.messenger as msgr
from itzi.cloud.metadata_storage import EnsembleDraft, list_ensemble_drafts
from itzi.grass.session import GrassParams, GrassSessionManager


class PackageMetadata(BaseModel):
    schema_version: Literal[1]
    format: Literal["precipient-zarr-tzst-v1"]
    project_id: str
    ensemble_id: str
    output_codes: tuple[str, ...] = Field(min_length=1)


@dataclass(frozen=True)
class MemberImport:
    index: int
    run_id: str
    output_names: tuple[tuple[str, str], ...]
    hmin: float
    count: int


def extract_package(path: Path, destination: Path) -> Path:
    """Extract only regular Zarr files under the package's one store root."""
    seen: set[str] = set()
    with tarfile.open(path, mode="r|zst") as archive:
        for entry in archive:
            name = PurePosixPath(entry.name)
            if (
                name.is_absolute()
                or ".." in name.parts
                or not name.parts
                or name.parts[0] != "results.zarr"
                or not (entry.isfile() or entry.isdir())
                or str(name) in seen
            ):
                raise ValueError(f"Invalid result package entry: {entry.name}")
            seen.add(str(name))
            archive.extract(entry, path=destination, filter="data")
    store = destination / "results.zarr"
    if not (store / "zarr.json").is_file():
        raise ValueError("Result package has no results.zarr group")
    return store


def native_record_times(
    time: xr.DataArray,
) -> tuple[TemporalType, tuple[datetime | timedelta, ...]]:
    """Recover source wall-clock times before native point-time registration."""
    values = time.values
    if time.dims != ("time",) or not values.size:
        raise ValueError("Result time must be a nonempty one-dimensional coordinate")
    if values.dtype.kind == "M":
        units = time.encoding.get("units", "")
        if not isinstance(units, str) or " since " not in units:
            raise ValueError("Absolute result times require offset-bearing CF units")
        origin = pd.Timestamp(units.split(" since ", 1)[1])
        # Older packages without an offset used UTC. Never consult the importing host's zone.
        zone = origin.tzinfo or UTC
        restored = pd.DatetimeIndex(values).tz_localize(UTC).tz_convert(zone).tz_localize(None)
        if restored.isna().any() or not np.all(np.diff(values) > np.timedelta64(0, "ns")):
            raise ValueError("Result times must be finite, unique, and increasing")
        if any(value.nanosecond for value in restored):
            raise ValueError("GRASS output times must have at most microsecond precision")
        return TemporalType.ABSOLUTE, tuple(value.to_pydatetime() for value in restored)
    if values.dtype.kind == "m":
        durations = pd.TimedeltaIndex(values)
        if (
            durations.isna().any()
            or np.any(values < np.timedelta64(0, "ns"))
            or not np.all(np.diff(values) > np.timedelta64(0, "ns"))
            or any(value.nanoseconds for value in durations)
        ):
            raise ValueError("Relative result times must be nonnegative, increasing microseconds")
        return TemporalType.RELATIVE, tuple(value.to_pytimedelta() for value in durations)
    raise ValueError("Result time must encode absolute timestamps or relative durations")


def validate_dataset(ds: xr.Dataset) -> tuple[PackageMetadata, CRS]:
    """Check the import contract before opening any output maps."""
    package = PackageMetadata.model_validate(ds.attrs.get("precipient:package"))
    codes = package.output_codes
    if len(set(codes)) != len(codes) or set(codes) - OUTPUT_ARRAY_KEYS:
        raise ValueError("Package contains duplicate or unsupported output codes")
    if any(name not in ds for name in (*codes, "time", "x", "y")):
        raise ValueError("Package is missing an output variable or coordinate")
    for name in ("member_label", "run_id", "simulation_id", "input_id", "output_count"):
        if name not in ds or ds[name].dims != ("member",):
            raise ValueError(f"Missing or invalid member metadata: {name}")
    runs = ds.run_id.values.tolist()
    if not runs or any(not isinstance(run, str) or not run for run in runs):
        raise ValueError("Package Run IDs must be nonempty strings")
    if len(set(runs)) != len(runs):
        raise ValueError("Package contains duplicate Run IDs")
    if "output_available" not in ds or ds.output_available.dims != ("member", "time"):
        raise ValueError("Missing or invalid output availability")
    flags = ds.output_available.values
    counts = ds.output_count.values
    if (
        counts.dtype.kind not in "iu"
        or np.any(counts < 0)
        or np.any(counts > ds.sizes["time"])
        or not np.array_equal(flags, np.arange(ds.sizes["time"])[None, :] < counts[:, None])
    ):
        raise ValueError("Output availability must match each member's available prefix")
    first = ds[codes[0]]
    crs = CRS.from_wkt(first.attrs.get("proj:wkt2", ""))
    if not (crs.is_projected or crs.is_engineering):
        raise ValueError("GRASS import requires a projected or engineering CRS")
    attrs = first.attrs
    transform = np.asarray(attrs.get("spatial:transform"), dtype=float)
    if (
        transform.shape != (6,)
        or not np.isfinite(transform).all()
        or transform[0] <= 0
        or transform[4] >= 0
        or transform[1] != 0
        or transform[3] != 0
        or attrs.get("spatial:registration", "pixel") != "pixel"
        or attrs.get("spatial:transform_type", "affine") != "affine"
    ):
        raise ValueError("GRASS import requires a north-up, pixel-registered affine grid")
    for name, size, start, step in (
        ("x", ds.sizes["x"], transform[2], transform[0]),
        ("y", ds.sizes["y"], transform[5], transform[4]),
    ):
        if (
            size < 3
            or ds[name].dims != (name,)
            or not np.allclose(
                ds[name].values, start + (np.arange(size) + 0.5) * step, rtol=0, atol=1e-7
            )
        ):
            raise ValueError(f"Invalid spatial coordinate: {name}")
    for code in codes:
        data = ds[code]
        if data.dims != ("member", "time", "y", "x") or data.dtype != np.float32:
            raise ValueError(f"Invalid raster dimensions or dtype: {code}")
        if data.attrs.get("spatial:dimensions") != ["y", "x"] or any(
            data.attrs.get(key) != attrs.get(key)
            for key in ("proj:wkt2", "spatial:transform", "spatial:registration")
        ):
            raise ValueError(f"Inconsistent raster georeferencing: {code}")
    return package, crs


def plan_members(
    ds: xr.Dataset,
    package: PackageMetadata,
    drafts: tuple[EnsembleDraft, ...],
    prefix: str | None,
    hmin: float | None,
) -> tuple[MemberImport, ...]:
    """Join presentation settings by Run identity, never by package member position."""
    if hmin is not None and (not math.isfinite(hmin) or hmin < 0):
        raise ValueError("hmin must be finite and nonnegative")
    members = []
    for index, run_id in enumerate(ds.run_id.values.tolist()):
        matches = [
            (draft, member)
            for draft in drafts
            for member, run in draft.run_ids.items()
            if run == run_id
        ]
        if len(matches) > 1:
            raise ValueError(f"Ambiguous saved output context for Run {run_id}")
        context = None
        if matches:
            draft, member = matches[0]
            if draft.input_id != str(ds.input_id.values[index]) or draft.simulation_ids.get(
                member
            ) != str(ds.simulation_id.values[index]):
                raise ValueError(f"Package source identity does not match saved Run {run_id}")
            context = draft.member_outputs.get(member)
        label = re.sub(r"[^a-zA-Z0-9_]", "_", str(ds.member_label.values[index]))[:40]
        suffix = hashlib.blake2b(run_id.encode(), digest_size=4).hexdigest()
        fallback = f"{prefix or 'cloud'}_{label}_{suffix}"
        names = tuple(
            (
                code,
                context.output_map_names[code]
                if prefix is None and context is not None and code in context.output_map_names
                else f"{fallback}_{code}",
            )
            for code in package.output_codes
        )
        threshold = hmin if hmin is not None else context.hmin if context else 0.0
        if context is None:
            msgr.message(f"Run {run_id}: using generated output names and hmin={threshold:g}")
        members.append(MemberImport(index, run_id, names, threshold, int(ds.output_count[index])))
    return tuple(members)


def import_result_package(
    path: Path,
    *,
    email: str | None = None,
    destination: GrassParams | None = None,
    prefix: str | None = None,
    hmin: float | None = None,
    overwrite: bool = False,
    expected_ensemble_id: str | None = None,
    expected_run_ids: tuple[str, ...] = (),
) -> None:
    """Import a downloaded or copied package without requiring API access."""
    destination = destination or GrassParams()
    with tempfile.TemporaryDirectory(prefix="itzi-results-") as temporary:
        store = extract_package(path, Path(temporary))
        with xr.open_zarr(store, consolidated=False, chunks=None, decode_timedelta=True) as ds:
            package, crs = validate_dataset(ds)
            if expected_ensemble_id is not None and package.ensemble_id != expected_ensemble_id:
                raise ValueError("Result package does not match the requested Ensemble")
            if expected_run_ids and set(ds.run_id.values.tolist()) != set(expected_run_ids):
                raise ValueError("Result package does not match the requested Runs")
            temporal_type, times = native_record_times(ds.time)
            drafts = tuple(
                draft
                for draft in list_ensemble_drafts(email)
                if draft.ensemble_id == package.ensemble_id
                and draft.project_id == package.project_id
            )
            members = plan_members(ds, package, drafts, prefix, hmin)
            if destination.grassdata is None and drafts:
                saved = {draft.grass_params for draft in drafts}
                if len(saved) != 1:
                    raise ValueError("Ambiguous saved GRASS destinations; specify a destination")
                saved_destination = saved.pop()
                destination = replace(
                    saved_destination,
                    grass_bin=destination.grass_bin or saved_destination.grass_bin,
                )
            if destination.grassdata is None and GrassSessionManager.current_context() is None:
                raise ValueError(
                    "No GRASS destination: use an active session or --gisdb, --project, --mapset"
                )
            _write_results(ds, crs, members, times, temporal_type, destination, overwrite)


def _write_results(
    ds: xr.Dataset,
    crs: CRS,
    members: tuple[MemberImport, ...],
    times: tuple[datetime | timedelta, ...],
    temporal_type: TemporalType,
    destination: GrassParams,
    overwrite: bool,
) -> None:
    transform = ds[members[0].output_names[0][0]].attrs["spatial:transform"]
    domain = DomainData(
        west=transform[2],
        north=transform[5],
        east=transform[2] + ds.sizes["x"] * transform[0],
        south=transform[5] + ds.sizes["y"] * transform[4],
        rows=ds.sizes["y"],
        cols=ds.sizes["x"],
        crs_wkt=crs.to_wkt(),
    )
    with GrassSessionManager(destination, domain=domain):
        import grass.script as gscript

        from itzi.ensemble.resolution import _validate_output_names
        from itzi.grass.interface import GrassInterface
        from itzi.grass.names import derived_record_name
        from itzi.grass.utils import check_output_files, get_crs_wkt, get_current_mapset
        from itzi.providers.grass_output import GrassRasterOutputProvider

        target_wkt = get_crs_wkt().strip()
        if target_wkt.startswith("XY location"):
            matches = crs.is_engineering
        else:
            matches = crs.equals(CRS.from_wkt(target_wkt), ignore_axis_order=True)
        if not matches:
            raise ValueError("Result package CRS does not match the destination GRASS project")
        mapset = get_current_mapset()
        names: set[str] = set()
        for member in members:
            _validate_output_names(dict(member.output_names), None, max(0, member.count - 1))
            for _, name in member.output_names:
                artifacts = [name] + [derived_record_name(name, i) for i in range(member.count)]
                if names.intersection(artifacts):
                    raise ValueError(f"Conflicting import output names: {name}")
                names.update(artifacts)
        start = times[0] if isinstance(times[0], datetime) else datetime.min.replace(tzinfo=UTC)
        end = times[-1] if isinstance(times[-1], datetime) else start + times[-1]
        previous_overwrite = gscript.overwrite()
        # GRASS spatial and temporal writers consult the same scoped environment flag.
        previous = os.environ.get("GRASS_OVERWRITE")
        os.environ["GRASS_OVERWRITE"] = "1" if overwrite or previous_overwrite else "0"
        try:
            check_output_files([f"{name}@{mapset}" for name in sorted(names)])
            interface = GrassInterface(start, end, np.float32)
            providers = tuple(
                (
                    member,
                    GrassRasterOutputProvider(
                        interface,
                        {code: f"{name}@{mapset}" for code, name in member.output_names},
                        member.hmin,
                        temporal_type,
                    ),
                )
                for member in members
                if member.count
            )
            for member, provider in providers:
                msgr.message(f"Run {member.run_id}: importing {member.count} records…")
                for record in range(member.count):
                    provider.write_arrays(
                        {
                            code: ds[code].isel(member=member.index, time=record).values
                            for code, _ in member.output_names
                        },
                        times[record],
                    )
                provider.finalize()
                msgr.message(
                    f"Run {member.run_id}: imported {', '.join(provider.out_map_names.values())}"
                )
            for member in members:
                if member.count < len(times):
                    msgr.warning(
                        f"Run {member.run_id}: partial output, "
                        f"imported {member.count}/{len(times)} records"
                    )
        finally:
            if previous is None:
                os.environ.pop("GRASS_OVERWRITE", None)
            else:
                os.environ["GRASS_OVERWRITE"] = previous
