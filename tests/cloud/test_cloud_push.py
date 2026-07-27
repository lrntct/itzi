from __future__ import annotations

import shutil
import sys
import types
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from itzi_core.const import TemporalType
from itzi_core.data_containers import SimulationConfig, SurfaceFlowParameters

from itzi.grass_session import GrassParams


def test_create_request_uses_project_slug(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from itzi.cloud import push
    from itzi.cloud.schemas import DomainInfo

    sim_config = SimulationConfig(
        start_time=datetime(2025, 1, 1, 12, tzinfo=UTC),
        end_time=datetime(2025, 1, 1, 13, tzinfo=UTC),
        record_step=timedelta(minutes=15),
        temporal_type=TemporalType.ABSOLUTE,
        input_map_names={"dem": "dem"},
        output_map_names={"h": "depth"},
        surface_flow_parameters=SurfaceFlowParameters(),
    )
    grass_params = GrassParams(grassdata=str(tmp_path), location="project", mapset="mapset")
    config_reader = types.SimpleNamespace(
        get_sim_params=lambda: sim_config,
        get_grass_params=lambda: grass_params,
    )
    dataset_path = tmp_path / "input.tgz"
    input_info = types.SimpleNamespace(
        sim_config=sim_config,
        dataset_path=dataset_path,
        dataset_hash="dataset-hash",
        dataset_bytes=1024,
        domain_info=DomainInfo(rows=2, cols=3, ewres=5.0, nsres=5.0),
    )
    monkeypatch.setattr(push, "ConfigReader", lambda path: config_reader)
    monkeypatch.setattr(
        push, "get_grass_params_from_env", lambda config_params: (grass_params, "config")
    )
    monkeypatch.setattr(push, "pack_input", lambda config, params: input_info)

    request, request_dataset_path, request_grass_params = push.create_request(
        "flood-studies", "sim.ini"
    )

    assert request.project_slug == "flood-studies"
    assert "project_id" not in request.model_dump()
    assert request.sim_config.stats_file == ""
    assert request.sim_config.surface_flow_parameters.vrouting == 0.1
    assert "hotstart_config" not in request.sim_config.model_dump()
    assert set(request.sim_config.model_dump()) == {
        "start_time",
        "end_time",
        "record_step",
        "temporal_type",
        "input_map_names",
        "output_map_names",
        "surface_flow_parameters",
        "stats_file",
        "dtinf",
        "infiltration_model",
        "swmm_inp",
        "drainage_output",
        "orifice_coeff",
        "free_weir_coeff",
        "submerged_weir_coeff",
    }
    assert set(request.sim_config.surface_flow_parameters.model_dump()) == {
        "hmin",
        "cfl",
        "theta",
        "g",
        "vrouting",
        "dtmax",
        "slope_threshold",
        "max_slope",
        "max_error",
    }
    assert request_dataset_path == dataset_path
    assert request_grass_params == grass_params


@pytest.mark.cloud
def test_pack_input_produces_stable_hash_for_identical_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from itzi.cloud import push

    class FakeGrassInterface:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.yr = 3
            self.xr = 4
            self.dx = 5.0
            self.dy = 6.0

    fake_grass_interface_module = types.ModuleType("itzi.providers.grass_interface")
    fake_grass_interface_module.GrassInterface = FakeGrassInterface
    monkeypatch.setitem(sys.modules, "itzi.providers.grass_interface", fake_grass_interface_module)
    monkeypatch.setattr(push, "GrassSessionManager", lambda *_args, **_kwargs: nullcontext())
    monkeypatch.setattr(
        push,
        "list_input_maps",
        lambda *_args, **_kwargs: {"mapset": {"raster": ["dem@mapset"], "strds": []}},
    )

    def fake_to_zarr(*_args: object, tempdir: Path, **_kwargs: object) -> None:
        tempdir.mkdir()
        (tempdir / "attrs.json").write_text("{}")
        (tempdir / "variables").mkdir()
        (tempdir / "variables" / "dem").write_text("placeholder")

    monkeypatch.setattr(push, "to_zarr", fake_to_zarr)

    sim_config = SimulationConfig(
        start_time=datetime(2025, 1, 1, 12, tzinfo=UTC),
        end_time=datetime(2025, 1, 1, 13, tzinfo=UTC),
        record_step=timedelta(minutes=15),
        temporal_type=TemporalType.ABSOLUTE,
        input_map_names={"dem": "dem@PERMANENT"},
        output_map_names={"h": "depth@PERMANENT"},
        surface_flow_parameters=SurfaceFlowParameters(),
    )
    grass_params = GrassParams(
        grassdata=str(tmp_path / "grassdb"),
        location="project",
        mapset="mapset",
    )

    first_input_info = push.pack_input(sim_config, grass_params)
    second_input_info = push.pack_input(sim_config, grass_params)

    try:
        assert first_input_info.dataset_path != second_input_info.dataset_path
        assert first_input_info.dataset_hash == second_input_info.dataset_hash
        assert first_input_info.dataset_bytes == second_input_info.dataset_bytes
        assert first_input_info.domain_info == second_input_info.domain_info
        assert first_input_info.sim_config == second_input_info.sim_config
        assert first_input_info.sim_config.input_map_names == {"dem": "dem"}
        assert first_input_info.sim_config.output_map_names == {"h": "depth"}
    finally:
        shutil.rmtree(first_input_info.dataset_path.parent, ignore_errors=True)
        shutil.rmtree(second_input_info.dataset_path.parent, ignore_errors=True)


def test_to_zarr_slices_relative_coordinates_in_their_declared_units(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import xarray as xr

    from itzi.cloud import push

    dataset = xr.Dataset(
        data_vars={
            "rain": (("start_time_rain", "y", "x"), [[[1]], [[2]], [[3]], [[4]]]),
            "inflow": (
                ("start_time_inflow", "y", "x"),
                [[[1]], [[2]], [[3]], [[4]]],
            ),
        },
        coords={
            "start_time_rain": (
                "start_time_rain",
                [0, 5, 10, 15],
                {"units": "minutes"},
            ),
            "start_time_inflow": (
                "start_time_inflow",
                [0, 300, 600, 900],
                {"units": "seconds"},
            ),
            "y": [0],
            "x": [0],
        },
        attrs={"history": "generated for test"},
    )
    relative_start = datetime.min.replace(tzinfo=UTC)
    sim_config = SimulationConfig(
        start_time=relative_start,
        end_time=relative_start + timedelta(minutes=10),
        record_step=timedelta(minutes=5),
        temporal_type=TemporalType.RELATIVE,
        input_map_names={"rain": "rain", "inflow": "inflow"},
        output_map_names={"h": "depth"},
        surface_flow_parameters=SurfaceFlowParameters(),
    )
    grass_params = GrassParams(
        grassdata=str(tmp_path / "grassdb"),
        location="project",
        mapset="mapset",
    )
    selected_datasets: list[xr.Dataset] = []

    monkeypatch.setattr(push, "read_all_maps", lambda *_args: dataset)
    monkeypatch.setattr(
        push.xr.Dataset,
        "to_zarr",
        lambda selected, _path: selected_datasets.append(selected),
    )

    push.to_zarr({}, grass_params, sim_config, tempdir=tmp_path / "input.zarr")

    selected = selected_datasets[0]
    assert selected.start_time_rain.values.tolist() == [0, 5, 10]
    assert selected.start_time_inflow.values.tolist() == [0, 300, 600]
