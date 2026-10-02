"""
Copyright (C) 2026 Laurent Courty

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
"""

from __future__ import annotations

import hashlib
import math
import shutil
import tarfile
import tempfile
import warnings
from compression.zstd import CompressionParameter
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from datetime import timedelta
from multiprocessing import get_context
from pathlib import Path
from typing import TypedDict

import numpy as np
import requests
import xarray as xr
import zarr
from itzi_core import TemporalType
from pydantic import BaseModel
from pyproj import CRS
from pyproj.exceptions import CRSError
from zarr.codecs import BytesCodec, ZstdCodec
from zarr.errors import UnstableSpecificationWarning

import itzi.messenger as msgr
from itzi.cloud import urls
from itzi.ensemble import load_yaml_stream
from itzi.ensemble.models import (
    ExpandedEnsemble,
    OutputTemplates,
    ResolvedSimulation,
    ValidationFailure,
)
from itzi.ensemble.resolution import resolve_ensemble
from itzi.grass.session import GrassSessionManager

RELATIVE_TIME_UNIT_SECONDS = {
    "seconds": 1,
    "minutes": 60,
    "hours": 3600,
    "days": 86400,
}

CHUNK_SIZE = 2048


class Encoding(TypedDict):
    chunks: tuple[int, ...]
    serializer: BytesCodec
    compressors: list[ZstdCodec]


class InputLimits(BaseModel):
    INPUT_MAX_ARCHIVE_BYTES: int
    INPUT_MAX_EXTRACTED_BYTES: int
    INPUT_MAX_ARCHIVE_ENTRIES: int
    INPUT_MAX_TAR_EXTENSION_BYTES: int
    INPUT_MAX_ZSTD_WINDOW_LOG: int
    INPUT_MAX_MEMBERS: int
    INPUT_MAX_MEMBER_LABEL_CODE_POINTS: int
    INPUT_MAX_DATA_VARIABLES: int
    INPUT_MIN_SPATIAL_COORDINATE_SAMPLES: int
    INPUT_MAX_2D_DOMAIN_CELLS: int
    INPUT_MAX_TIME_SAMPLES_PER_COORDINATE: int
    INPUT_MAX_XY_CHUNKS_PER_SLICE: int
    INPUT_MAX_LOGICAL_CHUNKS: int
    INPUT_MAX_CHUNK_ELEMENTS: int
    INPUT_MAX_NUMERIC_DTYPE_BYTES: int
    INPUT_MAX_METADATA_BYTES: int


class WriterExtension(BaseModel):
    name: str
    version: str


class SupportedVersions(BaseModel):
    xarray: list[str]
    zarr_python: list[str]
    accepted_extensions: list[WriterExtension]


class InputFormat(BaseModel):
    limits: InputLimits
    supported_versions: SupportedVersions


@dataclass(frozen=True)
class BuiltArchive:
    ensemble: ExpandedEnsemble
    path: Path
    sha256: str
    size_bytes: int
    simulations: tuple[ResolvedSimulation, ...]


def validate_crs(ds: xr.Dataset) -> None:
    """Validate that the dataset has a projected or engineering CRS in metres."""
    crs_wkt = ds.attrs.get("crs_wkt")
    if not isinstance(crs_wkt, str) or not crs_wkt.strip():
        raise ValueError("Dataset attribute 'crs_wkt' must contain a non-empty WKT string")
    try:
        crs = CRS.from_wkt(crs_wkt)
    except CRSError as error:
        raise ValueError("Dataset attribute 'crs_wkt' is not valid WKT") from error

    horizontal = crs.to_2d()
    if horizontal.is_bound and horizontal.source_crs is not None:
        horizontal = horizontal.source_crs
    coordinate_system = horizontal.coordinate_system
    units = coordinate_system.to_json_dict()["axis"][:2] if coordinate_system else []
    if (
        not (crs.is_projected or crs.is_engineering)
        or len(units) < 2
        or any(axis.unit_conversion_factor != 1 for axis in horizontal.axis_info[:2])
        or any(
            (unit := axis["unit"]) != "metre"
            and (not isinstance(unit, dict) or unit.get("type") != "LinearUnit")
            for axis in units
        )
    ):
        raise ValueError("Cloud Input requires projected or engineering metre-based CRS")


def convert_relative_time_coordinate(coord: xr.DataArray) -> xr.DataArray:
    """Convert an xarray-GRASS relative time coordinate to timedelta64."""
    if np.issubdtype(coord.dtype, np.timedelta64):
        return coord

    time_unit = coord.attrs.get("units")
    if not isinstance(time_unit, str) or time_unit not in RELATIVE_TIME_UNIT_SECONDS:
        supported_units = ", ".join(RELATIVE_TIME_UNIT_SECONDS)
        raise ValueError(
            f"Relative time coordinate <{coord.name}> uses unsupported unit "
            f"<{time_unit}>; supported units are {supported_units}"
        )
    unit_seconds = RELATIVE_TIME_UNIT_SECONDS[time_unit]

    converted_coord = coord.copy(data=coord.values * np.timedelta64(unit_seconds, "s"))
    converted_coord.attrs.pop("units")
    converted_coord.encoding["units"] = time_unit
    return converted_coord


def get_input_format(session_token: str) -> InputFormat:
    response = requests.get(
        f"{urls.get_execution_api_base()}/input-formats/precipient-itzi-input-v1",
        headers={"X-Session-Token": session_token},
        timeout=30,
    )
    response.raise_for_status()
    return InputFormat.model_validate(response.json())


def build_archives(path: str | Path, session_token: str) -> tuple[BuiltArchive, ...]:
    """Reject any invalid document/member before building or submitting archives."""
    stream = load_yaml_stream(path)
    if stream.failures:
        raise ValueError("; ".join(failure.format() for failure in stream.failures))
    if not stream.ensembles:
        raise ValueError("YAML stream contains no ensembles")
    resolved: list[tuple[ExpandedEnsemble, tuple[ResolvedSimulation, ...]]] = []
    for ensemble in stream.ensembles:
        cloud_members = tuple(
            replace(member, drainage=None, outputs=OutputTemplates(None, (), None, None))
            for member in ensemble.simulations
        )
        with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as executor:
            results = executor.submit(resolve_ensemble, cloud_members).result()
        failures = [result for result in results if isinstance(result, ValidationFailure)]
        if failures:
            raise ValueError(
                f"{ensemble.source.path} document {ensemble.source.document_index}: "
                + "; ".join(f"{failure.coordinates}: {failure.detail}" for failure in failures)
            )
        resolved.append(
            (
                ensemble,
                tuple(result for result in results if isinstance(result, ResolvedSimulation)),
            )
        )
    capability = get_input_format(session_token)
    archives: list[BuiltArchive] = []
    try:
        for ensemble, simulations in resolved:
            archives.append(build_archive(ensemble, simulations, capability))
    except Exception:
        for archive in archives:
            shutil.rmtree(archive.path.parent)
        raise
    return tuple(archives)


def _check_writer(capability: InputFormat) -> None:
    versions = capability.supported_versions
    if (
        xr.__version__ not in versions.xarray
        or zarr.__version__ not in versions.zarr_python
        or WriterExtension(name="fixed_length_utf32", version="1")
        not in versions.accepted_extensions
    ):
        raise ValueError(
            "Installed Xarray/Zarr writer or fixed_length_utf32 "
            "extension is not supported by the server"
        )


def _check_limit(value: int, maximum: int, description: str) -> None:
    if value > maximum:
        raise ValueError(f"{description} ({value}) exceeds limit ({maximum})")


def _warn_omissions(ensemble: ExpandedEnsemble) -> None:
    """No warning for the absence of CSV on purpose."""
    if any(member.drainage is not None for member in ensemble.simulations):
        msgr.warning(f"{ensemble.ensemble_id}: SWMM coupling/drainage will not run in the cloud")
    if ensemble.simulations[0].outputs.drainage_dataset:
        msgr.warning(f"{ensemble.ensemble_id}: drainage output will not run in the cloud")
    if not ensemble.simulations[0].outputs.raster_variables:
        msgr.warning(f"{ensemble.ensemble_id}: requesting water_depth as the cloud default output")


def source_names(simulations: tuple[ResolvedSimulation, ...]) -> dict[tuple[str, str], str]:
    """Assign archive variable names in the same order used by the Zarr writer."""
    sources: dict[tuple[str, str], str] = {}
    for sim in simulations:
        kinds = dict(sim.input_kinds)
        for role, identifier in sim.simulation_config.input_map_names.items():
            sources.setdefault((identifier, kinds[role]), f"source_{len(sources)}")
    return sources


def _dataset(
    simulations: tuple[ResolvedSimulation, ...],
    limits: InputLimits,
) -> xr.Dataset:
    first = simulations[0]
    domain = first.domain_data
    if any(
        sim.domain_data != domain
        or sim.effective_mask != first.effective_mask
        or sim.grass_params != first.grass_params
        for sim in simulations
    ):
        raise ValueError("Ensemble members must share one GRASS domain, mask, and mapset")
    _check_limit(len(simulations), limits.INPUT_MAX_MEMBERS, "members")
    labels = [sim.simulation_id for sim in simulations]
    _check_limit(
        max(map(len, labels)), limits.INPUT_MAX_MEMBER_LABEL_CODE_POINTS, "member label width"
    )
    if min(domain.cols, domain.rows) < limits.INPUT_MIN_SPATIAL_COORDINATE_SAMPLES:
        raise ValueError("GRASS region has too few spatial coordinate samples")
    _check_limit(domain.cols * domain.rows, limits.INPUT_MAX_2D_DOMAIN_CELLS, "domain cells")

    sources = source_names(simulations)
    _check_limit(len(sources), limits.INPUT_MAX_DATA_VARIABLES, "data variables")
    x = domain.west + (np.arange(domain.cols, dtype=np.float64) + 0.5) * (
        (domain.east - domain.west) / domain.cols
    )
    y = domain.north - (np.arange(domain.rows, dtype=np.float64) + 0.5) * (
        (domain.north - domain.south) / domain.rows
    )
    ds = xr.Dataset(
        coords={
            "member": ("member", np.arange(len(simulations), dtype=np.int16)),
            "member_label": ("member", np.asarray(labels, dtype=f"<U{max(map(len, labels))}")),
            "x": ("x", x),
            "y": ("y", y),
        }
    )
    crs_wkt = ""
    with GrassSessionManager(first.grass_params):
        from itzi.grass.interface import GrassInterface

        with GrassInterface(
            start_time=first.simulation_config.start_time,
            end_time=first.simulation_config.end_time,
            dtype=np.float32,
            region_id=first.grass_params.region,
            raster_mask_id=first.grass_params.mask,
            effective_mask=first.effective_mask,
        ) as interface:
            mask = interface.get_npmask()
            assert (
                first.grass_params.grassdata is not None
                and first.grass_params.location is not None
            )
            grass_project = Path(first.grass_params.grassdata) / first.grass_params.location
            for (identifier, kind), name in sources.items():
                map_name, mapset = identifier.split("@", 1)
                opened = xr.open_dataset(
                    grass_project / mapset,
                    backend_kwargs={
                        "raster": [identifier] if kind == "raster" else [],
                        "strds": [identifier] if kind == "strds" else [],
                    },
                )
                validate_crs(opened)
                crs_wkt = opened.attrs["crs_wkt"]
                variable = opened[map_name]
                time_dims = [dim for dim in variable.dims if dim not in ("x", "y")]
                if time_dims:
                    old_time = time_dims[0]
                    variable = variable.drop_vars(
                        [coord for coord in variable.coords if str(coord).startswith("end_time")],
                        errors="ignore",
                    )
                    if first.simulation_config.temporal_type == TemporalType.RELATIVE:
                        variable = variable.assign_coords(
                            {old_time: convert_relative_time_coordinate(variable[old_time])}
                        )
                        start, end = (
                            timedelta(0),
                            first.simulation_config.end_time - first.simulation_config.start_time,
                        )
                    else:
                        start, end = (
                            first.simulation_config.start_time,
                            first.simulation_config.end_time,
                        )
                    variable = variable.sel({old_time: slice(start, end)})
                    if not variable.sizes[old_time]:
                        raise ValueError(f"{identifier} has no samples during simulation time")
                    variable = variable.rename({old_time: f"time_{name}"})
                if first.effective_mask.mode == "explicit":
                    # xarray-grass reads through the active mapset MASK. Explicit masks
                    # override it, so recover the unmasked source before applying ours.
                    if kind == "raster":
                        data = interface.read_raster_map(identifier)
                    else:
                        import grass.temporal as tgis

                        map_rows = tgis.open_stds.open_old_stds(
                            identifier, "strds"
                        ).get_registered_maps(columns="id", order="start_time")
                        original_time = opened[map_name][old_time]
                        if first.simulation_config.temporal_type == TemporalType.RELATIVE:
                            original_time = convert_relative_time_coordinate(original_time)
                        selected = variable[f"time_{name}"]
                        indices = np.flatnonzero(np.isin(original_time.values, selected.values))
                        if len(map_rows) != original_time.size or len(indices) != selected.size:
                            raise ValueError(f"STRDS {identifier} changed during Input reading")
                        data = np.stack(
                            [interface.read_raster_map(map_rows[index][0]) for index in indices]
                        )
                    variable = variable.copy(data=data)
                variable = variable.assign_coords(x=x, y=y)
                variable = variable.where(~mask) if mask.any() else variable
                variable.attrs = {"units": variable.attrs.get("units", "")}
                ds[name] = variable
    ds.attrs = {
        "crs_wkt": crs_wkt,
        "itzi_dimension_names": {
            name: {
                **({"time": f"time_{name}"} if f"time_{name}" in ds[name].dims else {}),
                "y": "y",
                "x": "x",
            }
            for name in sources.values()
        },
    }
    validate_crs(ds)
    return ds


def _encoding(ds: xr.Dataset, limits: InputLimits) -> dict[str, Encoding]:
    encoding = {}
    chunk_count = 0
    for var_name in ds.variables:
        var = ds[var_name]
        var_name_str = str(var_name)
        if var_name_str != "member_label" and (
            var.dtype.kind not in "biufmM"
            or var.dtype.itemsize > limits.INPUT_MAX_NUMERIC_DTYPE_BYTES
        ):
            raise ValueError(f"Unsupported dtype for {var_name}: {var.dtype}")
        if var_name_str.startswith("time_"):
            _check_limit(
                var.size, limits.INPUT_MAX_TIME_SAMPLES_PER_COORDINATE, var_name_str + " samples"
            )
            if np.isnat(var.values).any() or not np.all(
                np.diff(var.values) > np.timedelta64(0, "ns")
            ):
                raise ValueError(f"{var_name} has missing, duplicate or unordered times")
        chunks = tuple(
            1 if dim == "member" or str(dim).startswith("time_") else min(size, CHUNK_SIZE)
            for dim, size in var.sizes.items()
        )
        _check_limit(
            math.prod(chunks), limits.INPUT_MAX_CHUNK_ELEMENTS, f"{var_name} chunk elements"
        )
        if var_name in ds.data_vars:
            _check_limit(
                math.ceil(var.sizes["x"] / chunks[var.dims.index("x")])
                * math.ceil(var.sizes["y"] / chunks[var.dims.index("y")]),
                limits.INPUT_MAX_XY_CHUNKS_PER_SLICE,
                f"{var_name} XY chunks",
            )
        chunk_count += math.prod(math.ceil(size / chunk) for size, chunk in zip(var.shape, chunks))
        encoding[var_name_str] = Encoding(
            chunks=chunks,
            serializer=BytesCodec(endian="little"),
            compressors=[ZstdCodec(level=3)],
        )
    _check_limit(chunk_count, limits.INPUT_MAX_LOGICAL_CHUNKS, "logical chunks")
    return encoding


def build_archive(
    ensemble: ExpandedEnsemble,
    simulations: tuple[ResolvedSimulation, ...],
    capability: InputFormat,
    destination: Path | None = None,
) -> BuiltArchive:
    """Write one archive for an expanded ensemble."""
    _check_writer(capability)
    if len(simulations) != len(ensemble.simulations):
        raise ValueError("All expanded members must resolve before building an Input")
    limits = capability.limits
    _warn_omissions(ensemble)
    ds = _dataset(simulations, limits)
    encoding = _encoding(ds, limits)
    owned = destination is None
    path = destination or Path(tempfile.mkdtemp(prefix="itzi-input-")) / "input.tzst"

    counts = [0, 0]

    def filter_entry(info: tarfile.TarInfo) -> tarfile.TarInfo:
        if not (info.isfile() or info.isdir()):
            raise ValueError(f"Unsupported tar entry: {info.name}")
        counts[0] += 1
        _check_limit(counts[0], limits.INPUT_MAX_ARCHIVE_ENTRIES, "tar entries")
        if info.isfile():
            counts[1] += info.size
            _check_limit(counts[1], limits.INPUT_MAX_EXTRACTED_BYTES, "extracted bytes")
        info.mtime = info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mode = 0o755 if info.isdir() else 0o644
        return info

    try:
        with tempfile.TemporaryDirectory(prefix="itzi-zarr-") as temp:
            zarr_path = Path(temp) / "input.zarr"
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"The data type \(FixedLengthUTF32\(",
                    category=UnstableSpecificationWarning,
                )
                ds.to_zarr(
                    zarr_path, mode="w", zarr_format=3, encoding=encoding, consolidated=False
                )
            metadata_size = sum(p.stat().st_size for p in zarr_path.rglob("zarr.json"))
            _check_limit(metadata_size, limits.INPUT_MAX_METADATA_BYTES, "Zarr metadata bytes")
            with tarfile.open(
                path,
                mode="w:zst",
                format=tarfile.USTAR_FORMAT,
                options={
                    CompressionParameter.compression_level: 3,
                    CompressionParameter.window_log: limits.INPUT_MAX_ZSTD_WINDOW_LOG,
                },
            ) as tar:
                tar.add(zarr_path, arcname="input.zarr", filter=filter_entry)
        _check_limit(path.stat().st_size, limits.INPUT_MAX_ARCHIVE_BYTES, "archive bytes")
        with path.open("rb") as file:
            sha256 = hashlib.file_digest(file, "sha256").hexdigest()
        return BuiltArchive(
            ensemble,
            path,
            sha256,
            path.stat().st_size,
            simulations,
        )
    except Exception:
        path.unlink(missing_ok=True)
        if owned:
            shutil.rmtree(path.parent)
        raise
