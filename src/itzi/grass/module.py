"""Adapt native GRASS options to the public ensemble schema."""

from __future__ import annotations

import itertools
import math
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal

import yaml

from itzi.ensemble.models import DIRECT_INPUT_KEYS, MAX_ENSEMBLE_MEMBERS, SourceDocument
from itzi.ensemble.resolution import _drainage_path, _validate_protected_file_output
from itzi.ensemble.schema import (
    DrainageSweepConfig,
    ParameterSweepConfig,
    YamlEnsembleDocumentV1,
)
from itzi.ensemble.yaml import expand_yaml_document
from itzi.grass.session import GrassSessionManager
from itzi.itzi import run_ensembles

INFILTRATION_KEYS = (
    "effective_porosity",
    "capillary_pressure",
    "hydraulic_conductivity",
    "soil_water_content",
)


def configuration(options: Mapping[str, str]) -> YamlEnsembleDocumentV1:
    """Build a reproducible configuration, anchoring filesystem paths to cwd."""
    if options.get("ground_elevation_strds"):
        raise ValueError("Ground elevation alternatives must be rasters")

    def maps(key: str) -> list[str] | None:
        raster, strds = options.get(key, ""), options.get(f"{key}_strds", "")
        return [
            value for values in (raster, strds) if values for value in values.split(",")
        ] or None

    inputs = {key: values for key in DIRECT_INPUT_KEYS if (values := maps(key))}
    model = options.get("infiltration", "none") or "none"
    rate = maps("infiltration_rate")
    ga = {key: values for key in INFILTRATION_KEYS if (values := maps(key))}
    if model == "none" and (rate or ga):
        raise ValueError("Infiltration inputs require an infiltration model")
    if model == "constant":
        if not rate or ga:
            raise ValueError("Prescribed infiltration requires only infiltration_rate")
        infiltration = [{"type": "constant", "rate": value} for value in rate]
    elif model == "green-ampt":
        if rate or not all(key in ga for key in INFILTRATION_KEYS[:3]):
            raise ValueError("Green–Ampt requires porosity, capillary pressure and conductivity")
        count = math.prod(len(values) for values in ga.values())
        if count > MAX_ENSEMBLE_MEMBERS:
            raise ValueError(
                f"Green–Ampt alternatives exceed the {MAX_ENSEMBLE_MEMBERS} member limit"
            )
        infiltration = [
            {"type": "green-ampt", **dict(zip(ga, values, strict=True))}
            for values in itertools.product(*ga.values())
        ]
    elif model == "none":
        infiltration = [{"type": "none"}]
    else:
        raise ValueError(f"Unknown infiltration model {model!r}")

    def numbers(key: str) -> list[float]:
        return [float(value) for value in options[key].split(",")]

    def path(value: str) -> str:
        return str(Path(value).expanduser().absolute())

    drainage = None
    if options.get("swmm_input"):
        drainage = {
            "swmm_input": [path(value) for value in options["swmm_input"].split(",")],
            **{
                key: numbers(key)
                for key in DrainageSweepConfig.model_fields
                if key != "swmm_input" and options.get(key)
            },
        }
    elif any(options.get(key) for key in DrainageSweepConfig.model_fields):
        raise ValueError("Drainage coupling parameters require swmm_input")
    outputs = {}
    if options.get("raster_prefix"):
        outputs["rasters"] = {
            "prefix": options["raster_prefix"],
            "variables": (options.get("variables") or "water_depth").split(","),
        }
    elif options.get("variables"):
        raise ValueError("Output variables require raster_prefix")
    for option, section, field in (
        ("statistics", "statistics", "file"),
        ("manifest", "manifest", "file"),
        ("drainage_output", "drainage", "vector_dataset"),
    ):
        if options.get(option):
            value = options[option]
            outputs[section] = {field: value if section == "drainage" else path(value)}
    return YamlEnsembleDocumentV1.model_validate(
        {
            "schema_version": 1,
            "ensemble": {"id": options.get("ensemble", "itzi")},
            "grass": {},
            "time": {
                key: options[key]
                for key in ("start", "end", "duration", "record_step")
                if options.get(key)
            },
            "input": {**inputs, "infiltration": infiltration},
            "parameters": {
                key: numbers(key) for key in ParameterSweepConfig.model_fields if options.get(key)
            },
            "drainage": drainage,
            "outputs": outputs,
        }
    )


def execute(options: Mapping[str, str], *, overwrite: bool, validate_only: bool) -> None:
    """Execute directly in the active GRASS interpreter, optionally saving YAML."""
    import grass.script as gs

    if GrassSessionManager.current_context() is None:
        raise RuntimeError("t.itzi requires an active GRASS session")
    document = configuration(options)
    saved = (
        Path(options["save_configuration"]).expanduser().resolve()
        if options.get("save_configuration")
        else None
    )
    if saved is not None and saved.suffix != ".yaml":
        raise ValueError("save_configuration must have a .yaml extension for the Itzï CLI")
    # Without an exported file, provenance is a stable virtual source in the working directory.
    source = SourceDocument(saved or Path.cwd() / "t.itzi.yaml", document_index=0)
    ensemble = expand_yaml_document(source, document)
    expected_kinds: dict[tuple[str, str], Literal["raster", "strds"]] = {}
    for key in (*DIRECT_INPUT_KEYS, "infiltration_rate", *INFILTRATION_KEYS):
        for kind, option in (("raster", key), ("strds", f"{key}_strds")):
            if values := options.get(option):
                expected_kinds.update(
                    (("infiltration" if key == "infiltration_rate" else key, value), kind)
                    for value in values.split(",")
                )
    ensemble = replace(
        ensemble,
        simulations=tuple(
            replace(
                simulation,
                expected_input_kinds=tuple(
                    (key, expected_kinds[key, value])
                    for key, value in (*simulation.input_maps, *simulation.infiltration.input_maps)
                ),
            )
            for simulation in ensemble.simulations
        ),
    )
    _validate_protected_file_output(
        saved, (_drainage_path(member) for member in ensemble.simulations)
    )
    gs.message(
        f"{len(ensemble.simulations)} member(s), Cartesian alternatives; each STRDS is one alternative"
    )
    old = os.environ.get("ITZI_VERBOSE")
    try:
        # GRASS's quiet/normal/verbose levels map onto the existing messenger levels.
        os.environ["ITZI_VERBOSE"] = str(min(4, max(0, gs.verbosity())))
        if saved is not None:
            run_ensembles((ensemble,), overwrite=overwrite, validate_only=True, direct=True)
            with saved.open("w" if overwrite else "x", encoding="utf-8") as stream:
                yaml.safe_dump(
                    document.model_dump(mode="json", exclude_none=True), stream, sort_keys=False
                )
        run_ensembles((ensemble,), overwrite=overwrite, validate_only=validate_only, direct=True)
    finally:
        if old is None:
            os.environ.pop("ITZI_VERBOSE", None)
        else:
            os.environ["ITZI_VERBOSE"] = old
