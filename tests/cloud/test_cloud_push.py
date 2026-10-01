from __future__ import annotations

import hashlib
import sys
import tarfile
import types
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from itzi_core.const import TemporalType
from itzi_core.data_containers import SimulationConfig, SurfaceFlowParameters

from itzi.grass.session import GrassParams

LOCAL_CRS_WKT = (
    'ENGCRS["Local engineering CRS",EDATUM["Unknown engineering datum"],'
    'CS[Cartesian,2],AXIS["x",east,ORDER[1],LENGTHUNIT["metre",1]],'
    'AXIS["y",north,ORDER[2],LENGTHUNIT["metre",1]]]'
)


@pytest.mark.parametrize(
    ("crs_wkt", "error_match"),
    [
        (None, "non-empty WKT"),
        ("", "non-empty WKT"),
        ("XY location (unprojected)", "not valid WKT"),
        ("not WKT", "not valid WKT"),
        (
            LOCAL_CRS_WKT.replace('LENGTHUNIT["metre",1]', 'LENGTHUNIT["foot",0.3048]'),
            "metre-based",
        ),
        (
            LOCAL_CRS_WKT.replace('LENGTHUNIT["metre",1]', 'ANGLEUNIT["radian",1]'),
            "metre-based",
        ),
    ],
)
def test_validate_crs_rejects_invalid_values(crs_wkt: str | None, error_match: str) -> None:
    import xarray as xr

    from itzi.cloud import archive

    attrs = {} if crs_wkt is None else {"crs_wkt": crs_wkt}

    with pytest.raises(ValueError, match=error_match):
        archive.validate_crs(xr.Dataset(attrs=attrs))


def test_validate_crs_accepts_engineering_crs() -> None:
    import xarray as xr
    from pyproj import CRS

    from itzi.cloud import archive

    for crs_wkt in (
        LOCAL_CRS_WKT,
        LOCAL_CRS_WKT.replace('LENGTHUNIT["metre",1]', 'LENGTHUNIT["custom-length",1]'),
        CRS.from_epsg(3857).to_wkt(),
        CRS.from_epsg(7415).to_wkt(),
        CRS.from_string(
            "+proj=utm +zone=10 +datum=WGS84 +towgs84=0,0,0 +units=m +type=crs"
        ).to_wkt(),
    ):
        archive.validate_crs(xr.Dataset(attrs={"crs_wkt": crs_wkt}))

    with pytest.raises(ValueError, match="metre-based"):
        archive.validate_crs(xr.Dataset(attrs={"crs_wkt": CRS.from_epsg(4326).to_wkt()}))


def test_relative_time_coordinate_without_units() -> None:
    import xarray as xr

    from itzi.cloud.archive import convert_relative_time_coordinate

    with pytest.raises(ValueError, match="unsupported unit <None>"):
        convert_relative_time_coordinate(xr.DataArray([0, 1], dims="time"))


def test_two_member_input_archive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import xarray as xr
    import zarr
    from itzi_core import DomainData

    from itzi.cloud import archive
    from itzi.ensemble.models import (
        ArtifactSummary,
        EffectiveMask,
        ResolvedSimulation,
        SourceDocument,
    )
    from itzi.ensemble.schema import YamlEnsembleDocumentV1
    from itzi.ensemble.yaml import expand_yaml_document

    source = SourceDocument(tmp_path / "input.yaml", 0)
    ensemble = expand_yaml_document(
        source,
        YamlEnsembleDocumentV1.model_validate(
            {
                "schema_version": 1,
                "ensemble": {"id": "study"},
                "grass": {},
                "time": {"duration": "00:10:00", "record_step": "00:05:00"},
                "input": {
                    "ground_elevation": "dem",
                    "friction": "n",
                    "rainfall_rate": ["rain_a", "rain_b"],
                },
                "parameters": {},
                "outputs": {"statistics": {"file": "stats.csv"}},
            }
        ),
    )
    grass = GrassParams(grassdata=str(tmp_path), location="project", mapset="mapset")
    domain = DomainData(
        north=50,
        south=0,
        east=50,
        west=0,
        rows=5,
        cols=5,
        crs_wkt="XY location (unprojected)",
    )
    simulations = tuple(
        ResolvedSimulation(
            simulation_id=f"sim-{i}",
            coordinates=member.coordinates,
            grass_params=grass,
            domain_data=domain,
            effective_mask=EffectiveMask("none", None),
            input_kinds=(
                ("ground_elevation", "raster"),
                ("friction", "raster"),
                ("rainfall_rate", "strds"),
            ),
            simulation_config=SimulationConfig(
                start_time=datetime.min,  # noqa: DTZ901 - core relative-time sentinel.
                end_time=datetime.min + timedelta(minutes=10),  # noqa: DTZ901
                record_step=timedelta(minutes=5),
                temporal_type=TemporalType.RELATIVE,
                input_map_names={
                    "ground_elevation": "dem@mapset",
                    "friction": "n@mapset",
                    "rainfall_rate": f"rain_{'ab'[i]}@mapset",
                },
                output_map_names={},
                surface_flow_parameters=SurfaceFlowParameters(),
            ),
            artifacts=ArtifactSummary((), None, None),
        )
        for i, member in enumerate(ensemble.simulations)
    )
    monkeypatch.setattr(archive, "GrassSessionManager", lambda *_args: nullcontext())

    fake_module = types.ModuleType("itzi.grass.interface")
    fake_module.GrassInterface = lambda **_kwargs: nullcontext(
        types.SimpleNamespace(get_npmask=lambda: np.zeros((5, 5), dtype=bool))
    )
    monkeypatch.setitem(sys.modules, "itzi.grass.interface", fake_module)

    def read_one(path: Path, *, backend_kwargs: dict[str, list[str]]) -> xr.Dataset:
        assert path == tmp_path / "project" / "mapset"
        name = (backend_kwargs["raster"] or backend_kwargs["strds"])[0].split("@")[0]
        if name.startswith("rain"):
            steps = [0, 5, 10] if name == "rain_a" else [0, 3, 6, 9]
            return xr.Dataset(
                {
                    name: (
                        (f"start_time_{name}", "y", "x"),
                        np.full((len(steps), 5, 5), 1 if name == "rain_a" else 2, dtype="f4"),
                    )
                },
                coords={
                    f"start_time_{name}": (f"start_time_{name}", steps, {"units": "minutes"}),
                    "x": np.arange(5) * 10 + 5,
                    "y": np.arange(5)[::-1] * 10 + 5,
                },
                attrs={"crs_wkt": LOCAL_CRS_WKT},
            )
        return xr.Dataset(
            {name: (("y", "x"), np.ones((5, 5), dtype="f4"))},
            coords={"x": np.arange(5) * 10 + 5, "y": np.arange(5)[::-1] * 10 + 5},
            attrs={"crs_wkt": LOCAL_CRS_WKT},
        )

    limits = archive.InputLimits.model_validate(
        dict.fromkeys(archive.InputLimits.model_fields, 1_000_000_000)
        | {
            "INPUT_MAX_ZSTD_WINDOW_LOG": 23,
            "INPUT_MIN_SPATIAL_COORDINATE_SAMPLES": 5,
        }
    )
    capability = archive.InputFormat(
        limits=limits,
        supported_versions=archive.SupportedVersions(
            xarray=[xr.__version__],
            zarr_python=[zarr.__version__],
            accepted_extensions=[archive.WriterExtension(name="fixed_length_utf32", version="1")],
        ),
    )
    with monkeypatch.context() as patch:
        patch.setattr(xr, "open_dataset", read_one)
        result = archive.build_archive(ensemble, simulations, capability, tmp_path / "input.tzst")
    raw = result.path.read_bytes()
    assert result.size_bytes == len(raw)
    assert result.sha256 == hashlib.sha256(raw).hexdigest()
    with tarfile.open(result.path, "r:zst") as tar:
        assert {p.name.split("/")[0] for p in tar.getmembers()} == {"input.zarr"}
        tar.extractall(tmp_path / "extracted", filter="data")
    ds = xr.open_zarr(tmp_path / "extracted" / "input.zarr", consolidated=False)
    assert ds.attrs["crs_wkt"] == LOCAL_CRS_WKT
    np.testing.assert_array_equal(ds.member, [0, 1])
    np.testing.assert_array_equal(ds.member_label, ["sim-0", "sim-1"])
    assert ds.member_label.dtype.kind == "U"
    assert set(ds.data_vars) == {"source_0", "source_1", "source_2", "source_3"}
    assert ds.source_2.sizes["time_source_2"] == 3
    assert ds.source_3.sizes["time_source_3"] == 4
    np.testing.assert_array_equal(
        ds.time_source_3.values, np.array([0, 180, 360, 540], dtype="timedelta64[s]")
    )
    assert ds.source_2.values[0, 0, 0] == 1
    assert ds.source_3.values[0, 0, 0] == 2
    assert ds.attrs["itzi_dimension_names"]["source_3"]["time"] == "time_source_3"
    assert ds.x.values[0] == 5 and ds.y.values[0] == 45
    for name in ds.variables:
        metadata = zarr.open_array(tmp_path / "extracted" / "input.zarr" / name, mode="r")
        assert [codec.to_dict()["name"] for codec in metadata.metadata.codecs] == ["bytes", "zstd"]
        assert metadata.metadata.codecs[0].to_dict()["configuration"]["endian"] == "little"
        if name in {"source_2", "source_3"}:
            assert metadata.chunks[0] == 1

    to_zarr = xr.Dataset.to_zarr

    def write_with_symlink(dataset, store, **kwargs):
        result = to_zarr(dataset, store, **kwargs)
        (Path(store) / "link").symlink_to("zarr.json")
        return result

    monkeypatch.setattr(xr, "open_dataset", read_one)
    with monkeypatch.context() as patch:
        patch.setattr(xr.Dataset, "to_zarr", write_with_symlink)
        with pytest.raises(ValueError, match="Unsupported tar entry"):
            archive.build_archive(ensemble, simulations, capability, tmp_path / "symlink.tzst")
    assert not (tmp_path / "symlink.tzst").exists()

    with pytest.raises(ValueError, match="data variables"):
        archive.build_archive(
            ensemble,
            simulations,
            capability.model_copy(
                update={"limits": limits.model_copy(update={"INPUT_MAX_DATA_VARIABLES": 3})}
            ),
            tmp_path / "invalid.tzst",
        )
    with pytest.raises(ValueError, match="writer"):
        archive.build_archive(
            ensemble,
            simulations,
            capability.model_copy(
                update={
                    "supported_versions": capability.supported_versions.model_copy(
                        update={"xarray": ["not-deployed"]}
                    )
                }
            ),
            tmp_path / "unsupported.tzst",
        )


def test_yaml_stream_rejects_invalid_document_before_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from itzi.cloud import archive

    path = tmp_path / "study.yaml"
    path.write_text(
        """\
schema_version: 1
ensemble: {id: valid}
grass: {}
time: {duration: '00:01:00', record_step: '00:00:30'}
input: {ground_elevation: z, friction: n}
parameters: {}
outputs: {}
---
schema_version: 1
ensemble: {id: invalid}
grass: {}
time: {duration: '00:01:00', record_step: '00:00:30'}
input: {ground_elevation: z}
parameters: {}
outputs: {}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(archive, "get_input_format", lambda _: pytest.fail("must not contact API"))
    with pytest.raises(ValueError, match="document 1"):
        archive.build_archives(path, "token")


def test_failed_member_resolution_never_contacts_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from itzi.cloud import archive
    from itzi.ensemble.models import ValidationFailure

    path = tmp_path / "study.yaml"
    path.write_text(
        """\
schema_version: 1
ensemble: {id: invalid}
grass: {}
time: {duration: '00:01:00', record_step: '00:00:30'}
input: {ground_elevation: z, friction: n}
parameters: {cfl: [0.2, 0.3]}
outputs: {}
""",
        encoding="utf-8",
    )

    failure = ValidationFailure((), "input_resolution", "missing z")
    monkeypatch.setattr(
        archive,
        "ProcessPoolExecutor",
        lambda **_kwargs: nullcontext(
            types.SimpleNamespace(
                submit=lambda *_: types.SimpleNamespace(result=lambda: (failure,))
            )
        ),
    )
    monkeypatch.setattr(archive, "get_input_format", lambda _: pytest.fail("must not contact API"))
    with pytest.raises(ValueError, match="missing z"):
        archive.build_archives(path, "token")


def test_input_format_uses_authenticated_execution_api(monkeypatch: pytest.MonkeyPatch) -> None:
    from itzi.cloud import archive

    calls = []
    monkeypatch.delenv("ITZI_CLOUD_API_BASE", raising=False)

    class Response:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {
                "limits": {name: 1000 for name in archive.InputLimits.model_fields},
                "supported_versions": {
                    "xarray": ["2026.7.0"],
                    "zarr_python": ["3.2.1"],
                    "accepted_extensions": [{"name": "fixed_length_utf32", "version": "1"}],
                },
            }

    monkeypatch.setattr(
        archive.requests, "get", lambda url, **kwargs: calls.append((url, kwargs)) or Response()
    )
    capability = archive.get_input_format("session-token")
    assert capability.limits.INPUT_MAX_MEMBERS == 1000
    assert calls == [
        (
            "http://localhost:8000/execution-api/v1/input-formats/precipient-itzi-input-v1",
            {"headers": {"X-Session-Token": "session-token"}, "timeout": 30},
        )
    ]
