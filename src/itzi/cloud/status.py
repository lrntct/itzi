"""
Copyright (C) 2025 Laurent Courty

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

from dataclasses import dataclass

import requests
from pydantic import TypeAdapter

import itzi.messenger as msgr
from itzi.cloud import urls
from itzi.cloud.metadata_storage import EnsembleDraft, list_ensemble_drafts
from itzi.cloud.schemas import InputDetailSchema, SimulationResponseSchema


@dataclass(frozen=True)
class EnsembleStatus:
    draft: EnsembleDraft
    input: InputDetailSchema | None
    simulations: tuple[SimulationResponseSchema, ...]


def get_ensemble_statuses(
    session_token: str, email: str, ensemble_id: str | None = None
) -> tuple[EnsembleStatus, ...]:
    """Read API facts for locally known submissions, optionally selecting one Ensemble."""
    drafts = list_ensemble_drafts(email)
    if ensemble_id is not None:
        drafts = tuple(draft for draft in drafts if draft.ensemble_id == ensemble_id)
        if not drafts:
            raise ValueError(f"No local submission found for Ensemble {ensemble_id} and {email}")

    statuses = []
    headers = {"X-Session-Token": session_token}
    for draft in drafts:
        if draft.ensemble_id is None or draft.input_id is None:
            statuses.append(EnsembleStatus(draft, None, ()))
            continue
        response = requests.get(
            urls.get_input_endpoint(draft.input_id), headers=headers, timeout=30
        )
        response.raise_for_status()
        input_detail = InputDetailSchema.model_validate(response.json())
        if (
            input_detail.input_id != draft.input_id
            or input_detail.ensemble_id != draft.ensemble_id
            or input_detail.project_id != draft.project_id
        ):
            raise ValueError(f"Input status does not match recorded Ensemble {draft.ensemble_id}")
        response = requests.get(
            urls.get_ensemble_simulations_endpoint(draft.ensemble_id), headers=headers, timeout=30
        )
        response.raise_for_status()
        simulations = TypeAdapter(tuple[SimulationResponseSchema, ...]).validate_python(
            response.json()
        )
        if any(sim.ensemble_id != draft.ensemble_id for sim in simulations):
            raise ValueError(f"Simulations do not match recorded Ensemble {draft.ensemble_id}")
        statuses.append(EnsembleStatus(draft, input_detail, simulations))
    return tuple(statuses)


def display_ensemble_table(statuses: tuple[EnsembleStatus, ...]) -> None:
    """Show one summary row per local cloud submission."""
    if not statuses:
        msgr.message("No locally recorded cloud submissions found for this account.")
        return

    def format_bytes(size_bytes: int) -> str:
        if size_bytes < 1000:
            return f"{size_bytes} B"
        size = float(size_bytes)
        for unit in ("kB", "MB", "GB", "TB", "PB"):
            size /= 1000
            if size < 1000:
                return f"{size:.1f} {unit}"
        return f"{size / 1000:.1f} EB"

    rows = [("ENSEMBLE_ID", "INPUT STATUS", "#2D_CELLS", "#MEMBERS", "STORAGE", "ACCEPTED AT")]
    for status in statuses:
        detail = status.input
        cells = "-"
        members = len(status.draft.member_labels)
        storage = "-"
        accepted = "-"
        if detail is not None:
            if detail.members is not None:
                members = len(detail.members)
            storage = format_bytes(
                detail.input_storage.reserved_bytes + detail.input_storage.accepted_bytes
            )
            if detail.acceptance is not None:
                sizes = {
                    coordinate.axis.lower(): coordinate.size
                    for coordinate in detail.acceptance.coordinate_summaries
                }
                if "x" in sizes and "y" in sizes:
                    cells = str(sizes["x"] * sizes["y"])
                accepted = detail.acceptance.accepted_at.astimezone().isoformat(
                    sep=" ", timespec="seconds"
                )
        rows.append(
            (
                status.draft.ensemble_id or "not recorded",
                detail.state if detail is not None else "pending locally",
                cells,
                str(members),
                storage,
                accepted,
            )
        )
    widths = tuple(max(len(row[index]) for row in rows) for index in range(len(rows[0])))
    for row in rows:
        msgr.message("  ".join(f"{cell:<{width}}" for cell, width in zip(row, widths)).rstrip())


def display_ensemble_statuses(statuses: tuple[EnsembleStatus, ...]) -> None:
    """Show Input facts and locally recorded Run requests, without execution-state guesses."""
    if not statuses:
        msgr.message("No locally recorded cloud submissions found for this account.")
        return
    for status in statuses:
        draft = status.draft
        msgr.message(
            f"Ensemble: {draft.ensemble_id or 'not recorded'}; Project: {draft.project_id}"
        )
        detail = status.input
        if detail is None:
            msgr.message("  Ensemble/Input creation pending locally; retry cloud push to resume.")
        else:
            changed = detail.state_changed_at.astimezone().isoformat(sep=" ", timespec="seconds")
            msgr.message(f"  Input: {detail.input_id}; state: {detail.state}; changed: {changed}")
            upload = detail.upload_confirmation
            if upload is not None:
                msgr.message(
                    f"  Confirmed upload: {upload.size_bytes} bytes; SHA-256: {upload.sha256}"
                )
            failure = detail.failure
            if failure is not None:
                msgr.message(f"  Failure: {failure.kind}/{failure.code}: {failure.message}")
                msgr.message(f"  Next action: {failure.next_action}")
            storage = detail.input_storage
            msgr.message(
                f"  Input storage (bytes): reserved={storage.reserved_bytes}; "
                f"accepted={storage.accepted_bytes}"
            )
            account = detail.account_storage
            msgr.message(
                f"  Account storage (bytes): entitlement={account.entitlement_bytes}; "
                f"reserved={account.reserved_bytes}; accepted={account.accepted_bytes}; "
                f"available={account.available_bytes}"
            )
            acceptance = detail.acceptance
            if acceptance is not None:
                accepted = acceptance.accepted_at.astimezone().isoformat(
                    sep=" ", timespec="seconds"
                )
                msgr.message(
                    f"  Acceptance: {acceptance.contract_revision}; accepted at: {accepted}"
                )
                metadata = acceptance.supported_metadata
                msgr.message(
                    f"  Zarr format: {metadata.zarr_format}; "
                    f"temporal type: {metadata.temporal_type}"
                )
                msgr.message(f"  CRS WKT: {acceptance.crs_wkt}")
                extent = acceptance.extent
                msgr.message(
                    f"  Cell-edge extent: x=[{extent.minimum_x}, {extent.maximum_x}]; "
                    f"y=[{extent.minimum_y}, {extent.maximum_y}]"
                )
                for variable in acceptance.variables:
                    msgr.message(
                        f"  Variable: {variable.name}; dtype={variable.dtype}; "
                        f"dimensions={','.join(variable.dimensions)}; shape={variable.shape}; "
                        f"scope={variable.member_scope}; time={variable.time_coordinate or '-'}"
                    )
                for coordinate in acceptance.coordinate_summaries:
                    msgr.message(
                        f"  Coordinate: {coordinate.name} ({coordinate.axis}); "
                        f"size={coordinate.size}; dtype={coordinate.dtype}; "
                        f"range=[{coordinate.first}, {coordinate.last}]; "
                        f"resolution={coordinate.resolution}; unit={coordinate.unit or '-'}"
                    )
                for coverage in acceptance.time_coverage:
                    msgr.message(
                        f"  Time coverage: {coverage.coordinate_name}; "
                        f"range=[{coverage.start}, {coverage.end}]; unit={coverage.unit or '-'}"
                    )
        stage = draft.upload_stage or "not recorded"
        msgr.message(f"  Local upload stage: {stage}")
        members = dict(enumerate(draft.member_labels))
        if detail is not None and detail.members is not None:
            members = {member.index: member.label for member in detail.members}
        simulations = {
            sim.member_index: sim for sim in status.simulations if sim.input_id == draft.input_id
        }
        members.update((index, sim.member_label) for index, sim in simulations.items())
        msgr.message(f"  {'MEMBER':<8} {'LABEL':<24} {'SIMULATION ID':<30} RECORDED RUN ID")
        for index, label in sorted(members.items()):
            simulation = simulations.get(index)
            simulation_id = simulation.simulation_id if simulation else "-"
            run_id = draft.run_ids.get(index, "-")
            msgr.message(f"  {index:<8} {label:<24} {simulation_id:<30} {run_id}")
        msgr.message(
            "  Run IDs are locally recorded submissions; "
            "execution state is unavailable from this API."
        )
