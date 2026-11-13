"""Exercise the installed native module in a real GRASS session (Python 3.14+)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
import xml.etree.ElementTree as ET
from pathlib import Path

import grass.script as gs
import pytest
import yaml
from grass.script.setup import SessionHandle

from itzi.ensemble.yaml import load_yaml_stream
from itzi.grass.module import configuration, execute
from itzi.simulation_runner import SimulationRunner

SOURCE = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.usefixtures("grass_5by5"),
    pytest.mark.skipif(sys.version_info < (3, 14), reason="t.itzi runtime requires Python 3.14+"),
]


@pytest.fixture(scope="session")
def addon(grass_xy_session: SessionHandle, tmp_path_factory: pytest.TempPathFactory) -> Path:
    prefix = tmp_path_factory.mktemp("addon")
    source = tmp_path_factory.mktemp("addon_source")
    for name in ("Makefile", "pyproject.toml", "README.md", "LICENSE", "MANIFEST.in"):
        shutil.copy2(SOURCE / name, source / name)
    for name in ("src", "addons"):
        shutil.copytree(SOURCE / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
    build_environment = tmp_path_factory.mktemp("addon_build")
    venv.EnvBuilder(with_pip=True).create(build_environment)
    env = os.environ.copy()
    env["PATH"] = os.pathsep.join(
        (str(build_environment / "bin"), str(prefix / "scripts"), env["PATH"])
    )
    gs.run_command("g.extension", extension="t.itzi", url=str(source), prefix=str(prefix), env=env)
    assert (prefix / "docs" / "html" / "t.itzi.html").is_file()
    installed_module = prefix / "etc" / "t.itzi" / "python" / "itzi" / "grass" / "module.py"
    assert (
        installed_module.read_bytes()
        == (SOURCE / "src" / "itzi" / "grass" / "module.py").read_bytes()
    )
    return prefix / "scripts" / "t.itzi"


def invoke(addon: Path, *arguments: str, **options: str) -> subprocess.CompletedProcess[str]:
    defaults = {
        "ground_elevation": "z",
        "friction": "n",
        "duration": "00:00:02",
        "record_step": "00:00:01",
        "raster_prefix": "flood_{simulation}",
        "water_depth": "start_h",
    }
    defaults.update(options)
    env = {**os.environ, "GRASS_VERBOSE": "2"}
    return subprocess.run(
        [
            sys.executable,
            "-S",
            str(addon),
            *arguments,
            *(f"{key}={value}" for key, value in defaults.items() if value),
        ],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def manifest(tmp_path: Path) -> dict:
    return yaml.safe_load((tmp_path / "results" / "itzi.manifest.yaml").read_text())


def test_installed_single_member_and_overwrite(addon: Path, tmp_path: Path) -> None:
    first = invoke(addon)
    assert first.returncode == 0, first.stderr
    members = manifest(tmp_path)["members"]
    assert len(members) == 1
    assert members[0]["status"] == "completed"
    output = members[0]["artifacts"]["rasters"]["water_depth"]
    assert gs.find_file(output.partition("@")[0] + "_0001", element="cell")["name"]
    assert invoke(addon).returncode != 0
    overwritten = invoke(addon, "--overwrite")
    assert overwritten.returncode == 0, overwritten.stderr


def test_two_by_two_sweep_and_yaml_replay(addon: Path, tmp_path: Path) -> None:
    gs.mapcalc("n2=0.06")
    gs.mapcalc("rain2=20")
    result = invoke(
        addon,
        friction="n,n2",
        rainfall_rate="rainfall,rain2",
        save_configuration="setup.yaml",
        statistics="stats/{simulation}.csv",
    )
    assert result.returncode == 0, result.stderr
    members = manifest(tmp_path)["members"]
    assert len(members) == 4
    assert all(member["status"] == "completed" for member in members)
    assert len({member["simulation_id"] for member in members}) == 4
    loaded = load_yaml_stream(tmp_path / "setup.yaml")
    assert not loaded.failures
    assert len(loaded.ensembles[0].simulations) == 4
    assert configuration(
        {
            "ground_elevation": "z",
            "friction": "n,n2",
            "rainfall_rate": "rainfall,rain2",
            "water_depth": "start_h",
            "duration": "00:00:02",
            "record_step": "00:00:01",
            "raster_prefix": "flood_{simulation}",
            "statistics": "stats/{simulation}.csv",
        }
    ).model_dump(exclude_none=True) == yaml.safe_load((tmp_path / "setup.yaml").read_text())
    replay = subprocess.run(
        [sys.executable, "-m", "itzi.itzi", "run", "-o", str(tmp_path / "setup.yaml")],
        cwd=tmp_path.parent,
        text=True,
        capture_output=True,
    )
    assert replay.returncode == 0, replay.stderr
    assert {member["simulation_id"] for member in manifest(tmp_path)["members"]} == {
        member["simulation_id"] for member in members
    }


def test_validate_only_and_collision(addon: Path, tmp_path: Path) -> None:
    before = gs.list_strings("raster")
    result = invoke(addon, "-v", cfl="0.5,0.6")
    assert result.returncode == 0, result.stderr
    assert "Cartesian" in result.stderr
    assert not (tmp_path / "results").exists()
    assert gs.list_strings("raster") == before
    result = invoke(addon, "-v", cfl="0.5,0.6", raster_prefix="collision")
    assert result.returncode != 0
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("input_key", ["rainfall_rate", "infiltration_rate"])
def test_strds_is_one_temporal_alternative(
    addon: Path, tmp_path: Path, absolute: bool, mixed: bool, input_key: str
) -> None:
    gs.run_command(
        "t.create",
        output="rain_series",
        type="strds",
        temporaltype="absolute" if absolute else "relative",
        title="rain",
        description="rain",
    )
    gs.mapcalc("rain2=20")
    gs.run_command(
        "t.register",
        flags="i",
        input="rain_series",
        type="raster",
        maps="rainfall,rain2",
        start="2026-01-01 00:00:00" if absolute else "0",
        increment="1 seconds" if absolute else "1",
        **({} if absolute else {"unit": "seconds"}),
    )
    options = {f"{input_key}_strds": "rain_series"}
    if absolute:
        options["start"] = "2026-01-01T00:00:00"
    if input_key == "infiltration_rate":
        options["infiltration"] = "constant"
    if mixed:
        gs.mapcalc("n2=0.06")
        options.update(
            {input_key: "rainfall", "friction": "n,n2", "save_configuration": "mixed.yaml"}
        )
    result = invoke(addon, **options)
    assert result.returncode == 0, result.stderr
    members = manifest(tmp_path)["members"]
    assert len(members) == (4 if mixed else 1)
    assert all(member["status"] == "completed" for member in members)
    if mixed:
        replay = subprocess.run(
            [sys.executable, "-m", "itzi.itzi", "run", "-o", str(tmp_path / "mixed.yaml")],
            text=True,
            capture_output=True,
        )
        assert replay.returncode == 0, replay.stderr
        assert {member["simulation_id"] for member in manifest(tmp_path)["members"]} == {
            member["simulation_id"] for member in members
        }


def test_green_ampt_cartesian_maps(addon: Path, tmp_path: Path) -> None:
    for name, value in (("p1", 0.3), ("p2", 0.4), ("cap", 0.1), ("k1", 1), ("k2", 2)):
        gs.mapcalc(f"{name}={value}")
    result = invoke(
        addon,
        infiltration="green-ampt",
        effective_porosity="p1,p2",
        capillary_pressure="cap",
        hydraulic_conductivity="k1,k2",
    )
    assert result.returncode == 0, result.stderr
    members = manifest(tmp_path)["members"]
    assert len(members) == 4
    assert all(member["status"] == "completed" for member in members)


def test_parser_selectors_and_runtime_without_site_packages(addon: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-S", str(addon), "--interface-description"],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    task = ET.fromstring(result.stdout)
    assert task.find("parameter[@name='ground_elevation_strds']") is None
    ground_elevation = task.find("parameter[@name='ground_elevation']")
    assert ground_elevation is not None
    assert ground_elevation.attrib["required"] == "yes"
    for name, prompt in (("ground_elevation", "raster"), ("rainfall_rate_strds", "strds")):
        parameter = task.find(f"parameter[@name='{name}']")
        assert parameter is not None
        assert parameter.attrib["multiple"] == "yes"
        gisprompt = parameter.find("gisprompt")
        assert gisprompt is not None
        assert gisprompt.attrib["prompt"] == prompt
    assert {(item.text or "").strip() for item in task.findall("parameter/guisection")} == {
        "Inputs",
        "Time",
        "Infiltration",
        "Drainage",
        "Outputs",
        "Advanced",
    }
    installed = subprocess.run(
        [
            sys.executable,
            "-S",
            str(addon),
            "ground_elevation=z",
            "friction=n",
            "duration=00:00:02",
            "record_step=00:00:01",
        ],
        text=True,
        capture_output=True,
    )
    assert installed.returncode == 0, installed.stderr


@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_direct_lifecycle_restores_region_mask_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException] | None,
) -> None:
    import grass.temporal as tgis

    gs.set_raise_on_error(False)
    tgis.set_raise_on_error(False)
    gs.run_command("g.region", region="3by5")
    gs.mapcalc("initial_uniform=0.2")
    gs.run_command("r.mask", raster="5by3")
    region = gs.region()
    environment = os.environ.copy()
    original = SimulationRunner.run

    def run(runner: SimulationRunner) -> SimulationRunner:
        if failure:
            runner.step()
            raise failure("injected member failure")
        return original(runner)

    monkeypatch.setattr(SimulationRunner, "run", run)
    options = {
        "ground_elevation": "z",
        "friction": "n",
        "water_depth": "initial_uniform",
        "duration": "00:00:02",
        "record_step": "00:00:01",
        "cfl": "0.5,0.6",
        "raster_prefix": "lifecycle_{simulation}",
    }
    if failure:
        with pytest.raises(KeyboardInterrupt if failure is KeyboardInterrupt else RuntimeError):
            execute(options, overwrite=False, validate_only=False)
    else:
        execute(options, overwrite=False, validate_only=False)
    assert gs.region() == region
    assert os.environ == environment
    assert not gs.get_raise_on_error()
    assert not tgis.get_raise_on_error()
    assert gs.find_file("MASK", element="cell")["name"]
    statuses = [member["status"] for member in manifest(tmp_path)["members"]]
    assert statuses == (
        ["completed", "completed"]
        if not failure
        else ["execution_failed", "planned"]
        if failure is KeyboardInterrupt
        else ["execution_failed", "execution_failed"]
    )
    for member in manifest(tmp_path)["members"]:
        if member["status"] == "planned":
            continue
        output = member["artifacts"]["rasters"]["water_depth"].partition("@")[0] + "_0000"
        info = gs.raster_info(output)
        assert (info["rows"], info["cols"]) == ("3", "5")
        assert int(gs.parse_command("r.univar", flags="g", map=output)["n"]) == 9
    if failure:
        monkeypatch.setattr(SimulationRunner, "run", original)
        execute(
            {
                **options,
                "raster_prefix": "recovery_{simulation}",
                "manifest": "recovery.manifest.yaml",
            },
            overwrite=False,
            validate_only=False,
        )
        recovered = yaml.safe_load((tmp_path / "recovery.manifest.yaml").read_text())
        assert all(member["status"] == "completed" for member in recovered["members"])
        assert gs.region() == region
        assert os.environ == environment
    gs.run_command("r.mask", flags="r")


def test_failed_member_command_exit_and_manifest(addon: Path, tmp_path: Path) -> None:
    for name, duration in (("short_rain", "1"), ("full_rain", "2")):
        gs.mapcalc(f"{name}_map=10")
        gs.run_command(
            "t.create",
            output=name,
            type="strds",
            temporaltype="relative",
            title=name,
            description=name,
        )
        gs.run_command(
            "t.register",
            flags="i",
            input=name,
            type="raster",
            maps=f"{name}_map",
            start="0",
            increment=duration,
            unit="seconds",
        )
    result = invoke(addon, rainfall_rate_strds="short_rain,full_rain")
    assert result.returncode != 0, result.stderr
    assert sorted(member["status"] for member in manifest(tmp_path)["members"]) == [
        "completed",
        "validation_failed",
    ]


def test_drainage_alternatives_and_export_protection(
    addon: Path,
    tmp_path: Path,
    test_data_path: str,
) -> None:
    swmm = Path(test_data_path) / "tutorial_files" / "tutorial_drainage.inp"
    second = tmp_path / "second.inp"
    second.write_bytes(swmm.read_bytes())
    destination = tmp_path / "export"
    destination.mkdir()
    options = {
        "swmm_input": f"{swmm},second.inp",
        "orifice_coeff": "0.6,0.7",
        "save_configuration": "export/setup.yaml",
        "statistics": "stats/{simulation}.csv",
    }
    result = invoke(addon, "-v", **options)
    assert result.returncode == 0, result.stderr
    saved = destination / "setup.yaml"
    content = saved.read_bytes()
    expanded = load_yaml_stream(saved)
    assert not expanded.failures
    assert len(expanded.ensembles[0].simulations) == 4
    assert not (destination / "results").exists()
    assert not (tmp_path / "stats").exists()
    assert invoke(addon, "-v", **options).returncode != 0
    assert saved.read_bytes() == content
    overwrite = invoke(addon, "-v", "--overwrite", **options)
    assert overwrite.returncode == 0, overwrite.stderr


def test_unsupported_interpreter_error_before_library_import(addon: Path) -> None:
    code = """
import runpy
import sys
import grass.script as gs
parse = gs.parser
def parser():
    result = parse()
    sys.version_info = (3, 13)
    return result
gs.parser = parser
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            code,
            str(addon),
            "ground_elevation=z",
            "friction=n",
            "duration=00:00:02",
            "record_step=00:00:01",
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "requires Python 3.14" in result.stderr
    assert "Cannot import" not in result.stderr


@pytest.mark.parametrize("option,value", [("friction", "n,missing"), ("friction_strds", "n")])
def test_bad_inputs_are_reported_as_member_failures(
    addon: Path,
    tmp_path: Path,
    option: str,
    value: str,
) -> None:
    result = invoke(addon, **{"friction": "", option: value})
    assert result.returncode != 0
    statuses = sorted(member["status"] for member in manifest(tmp_path)["members"])
    assert statuses == (
        ["completed", "validation_failed"] if option == "friction" else ["validation_failed"]
    )
    if option == "friction_strds":
        assert "expected strds" in str(manifest(tmp_path))


def test_mixed_alternatives_keep_source_kind_validation(addon: Path, tmp_path: Path) -> None:
    result = invoke(addon, rainfall_rate="rainfall", rainfall_rate_strds="start_h")
    assert result.returncode != 0
    members = manifest(tmp_path)["members"]
    assert sorted(member["status"] for member in members) == ["completed", "validation_failed"]
    assert "expected strds" in str(members)


@pytest.mark.parametrize("depth", ["water_depth", "water_depth_strds"])
@pytest.mark.parametrize("surface", ["water_surface_elevation", "water_surface_elevation_strds"])
def test_initial_condition_types_remain_exclusive(addon: Path, depth: str, surface: str) -> None:
    result = invoke(addon, **{"water_depth": "", depth: "start_h", surface: "start_h"})
    assert result.returncode != 0
    assert "mutually exclusive" in result.stderr


@pytest.mark.parametrize("alias", [False, True])
def test_yaml_export_cannot_destroy_swmm_input(
    addon: Path,
    tmp_path: Path,
    test_data_path: str,
    alias: bool,
) -> None:
    original = tmp_path / "network.inp"
    original.write_bytes(
        (Path(test_data_path) / "tutorial_files" / "tutorial_drainage.inp").read_bytes()
    )
    exported = tmp_path / "network.yaml"
    if alias:
        exported.hardlink_to(original)
    else:
        original.rename(exported)
        original = exported
    content = original.read_bytes()
    result = invoke(
        addon, "--overwrite", swmm_input=str(original), save_configuration=str(exported)
    )
    assert result.returncode != 0
    assert "protected input" in result.stderr
    assert original.read_bytes() == content
