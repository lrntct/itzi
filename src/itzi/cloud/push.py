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

import base64
import hashlib
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BufferedReader, FileIO
from typing import Literal

import requests
from itzi_core import TemporalType

import itzi.messenger as msgr
from itzi.cloud import urls
from itzi.cloud.archive import BuiltArchive, source_names
from itzi.cloud.metadata_storage import (
    EnsembleDraft,
    get_or_create_ensemble_draft,
    reset_ensemble_draft,
    save_ensemble_draft,
)
from itzi.cloud.schemas import (
    EnsembleInputResponseSchema,
    InputConfirmationSchema,
    InputUploadInstructionSchema,
    InputUploadStatusSchema,
    RequestErrorResponseSchema,
    RunResponseSchema,
    SimulationResponseSchema,
)

VALIDATION_WAIT_SECONDS = 60
VALIDATION_POLL_SECONDS = 2
RESULT_REPOSITORY_WAIT_SECONDS = 10
RESULT_REPOSITORY_POLL_SECONDS = 2
PROGRESS_REPORT_SECONDS = 5


class _UploadReader(BufferedReader):
    """Report transfer progress while preserving the file's length and seek support."""

    def __init__(self, archive: BuiltArchive, input_id: str) -> None:
        super().__init__(FileIO(archive.path, "rb"))
        self._size_bytes = archive.size_bytes
        self._input_id = input_id
        self._last_report = time.monotonic()

    def read(self, size: int | None = -1, /) -> bytes:
        chunk = super().read(size)
        now = time.monotonic()
        sent = self.tell()
        if (
            chunk
            and sent < self._size_bytes
            and now - self._last_report >= PROGRESS_REPORT_SECONDS
        ):
            msgr.message(
                f"Input {self._input_id}: uploading — "
                f"{sent:,}/{self._size_bytes:,} bytes ({sent / self._size_bytes:.0%})"
            )
            self._last_report = now
        return chunk


def create_ensemble(
    archive: BuiltArchive, *, project_id: str, email: str, session_token: str, force: bool = False
) -> EnsembleDraft:
    """Create or resume an Ensemble, replacing cached IDs if deleted."""
    source = archive.ensemble.source
    if source.yaml_sha256 is None:
        raise ValueError("Cloud push requires a parsed YAML document")
    grass = archive.simulations[0].grass_params
    draft = get_or_create_ensemble_draft(
        email,
        project_id,
        tuple(sim.simulation_id for sim in archive.simulations),
        replace(grass, region=None, mask=None),
        archive.sha256,
        source.yaml_sha256,
        force=force,
    )
    if draft.ensemble_id is not None and draft.input_id is not None:
        msgr.message(
            f"{archive.ensemble.ensemble_id}: checking saved Ensemble {draft.ensemble_id}…"
        )
        response = requests.get(
            urls.get_input_endpoint(draft.input_id),
            headers={"X-Session-Token": session_token},
            timeout=30,
        )
        if response.status_code != 404:
            response.raise_for_status()
            msgr.message(f"{archive.ensemble.ensemble_id}: reusing Ensemble {draft.ensemble_id}")
            return draft
        msgr.message(f"Saved Ensemble {draft.ensemble_id} no longer exists; recreating…")
        draft = reset_ensemble_draft(draft)

    msgr.message(f"{archive.ensemble.ensemble_id}: creating cloud Ensemble…")
    response = requests.post(
        urls.get_ensembles_endpoint(project_id),
        headers={"X-Session-Token": session_token, "Idempotency-Key": draft.idempotency_key},
        json={
            "name": archive.ensemble.ensemble_id,
            "description": archive.ensemble.ensemble_description or "",
        },
        timeout=30,
    )
    if response.status_code == 409:
        raise ValueError(f"Ensemble creation conflict: {response.json()['detail']}")
    if response.status_code == 422:
        raise ValueError(f"Invalid Ensemble metadata: {response.json()['detail']}")
    response.raise_for_status()
    created = EnsembleInputResponseSchema.model_validate(response.json())
    draft = draft.model_copy(
        update={"ensemble_id": created.ensemble_id, "input_id": created.input_id}
    )
    save_ensemble_draft(draft)
    msgr.message(f"Ensemble {draft.ensemble_id} ready, Input {draft.input_id}")
    return draft


def _record_upload(
    draft: EnsembleDraft,
    stage: Literal["uploaded", "confirmed"],
    transfer_id: str,
    confirmation_state: str | None = None,
) -> EnsembleDraft:
    updated = draft.model_copy(
        update={
            "upload_stage": stage,
            "transfer_id": transfer_id,
            "confirmation_state": confirmation_state,
        }
    )
    save_ensemble_draft(updated)
    return updated


def _confirmed_on_server(
    draft: EnsembleDraft, session_token: str, archive: BuiltArchive
) -> str | None:
    assert draft.input_id is not None
    response = requests.get(
        urls.get_input_endpoint(draft.input_id),
        headers={"X-Session-Token": session_token},
        timeout=30,
    )
    response.raise_for_status()
    status = InputUploadStatusSchema.model_validate(response.json())
    if status.input_id != draft.input_id:
        raise ValueError("Input status refers to another Input")
    if status.upload_confirmation is None:
        if status.state in ("validating", "accepted", "failed"):
            raise ValueError(f"Input {draft.input_id} is {status.state} without this upload")
        return None
    if (
        status.upload_confirmation.size_bytes != archive.size_bytes
        or status.upload_confirmation.sha256 != archive.sha256
    ):
        raise ValueError(f"Input {draft.input_id} was confirmed with a different archive")
    return status.state


def upload_input(archive: BuiltArchive, draft: EnsembleDraft, session_token: str) -> EnsembleDraft:
    """Upload exactly one archive and confirm it; acceptance is a later step."""
    if draft.input_id is None:
        raise ValueError("Create the Ensemble before uploading its Input")
    if draft.upload_stage == "confirmed":
        msgr.message(f"Input {draft.input_id}: upload already confirmed; reusing it")
        return draft

    input_url = urls.get_input_endpoint(draft.input_id)
    headers = {"X-Session-Token": session_token}
    if draft.upload_stage is not None:
        msgr.message(f"Input {draft.input_id}: checking saved upload on the server…")
        state = _confirmed_on_server(draft, session_token, archive)
        if state is not None:
            msgr.message(f"Input {draft.input_id}: upload already confirmed on server ({state})")
            return _record_upload(draft, "confirmed", draft.transfer_id or "", state)

    if draft.upload_stage == "uploaded":
        if draft.transfer_id is None:
            raise ValueError("Uploaded Input has no saved transfer ID")
        transfer_id = draft.transfer_id
        msgr.message(f"Input {draft.input_id}: archive already uploaded; resuming confirmation")
    else:
        msgr.message(f"Input {draft.input_id}: checking archive checksums…")
        md5 = hashlib.md5()  # API checksum contract
        sha256 = hashlib.sha256()
        with archive.path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                md5.update(chunk)
                sha256.update(chunk)
        if sha256.hexdigest() != archive.sha256:
            raise ValueError("Archive SHA-256 changed since building the Input")
        msgr.message(f"Input {draft.input_id}: requesting upload instructions…")
        response = requests.post(
            f"{input_url}/upload-instructions",
            headers=headers,
            json={
                "size_bytes": archive.size_bytes,
                "content_md5_base64": base64.b64encode(md5.digest()).decode("ascii"),
            },
            timeout=30,
        )
        if response.status_code == 429:
            payload = response.json()
            raise ValueError(
                f"Input storage entitlement exceeded: {payload['detail']} "
                f"(required {payload['required_bytes']}, "
                "available {payload['available_bytes']} bytes)"
            )
        response.raise_for_status()
        instruction = InputUploadInstructionSchema.model_validate(response.json())
        if instruction.input_id != draft.input_id or instruction.size_bytes != archive.size_bytes:
            raise ValueError("Upload instructions do not match this Input archive")
        if instruction.expires_at.tzinfo is None or instruction.expires_at <= datetime.now(UTC):
            raise ValueError("Signed upload URL expired; retry cloud push")
        if any(
            key.lower() == "content-type" and value.lower() != "application/zstd"
            for key, value in instruction.headers.items()
        ):
            raise ValueError("Signed upload Content-Type does not match application/zstd")
        msgr.message(f"Input {draft.input_id}: uploading — 0/{archive.size_bytes:,} bytes (0%)")
        try:
            with _UploadReader(archive, draft.input_id) as file:
                put = requests.put(
                    instruction.url,
                    headers={"Content-Type": "application/zstd", **instruction.headers},
                    data=file,
                    timeout=300,
                )
            put.raise_for_status()
        except requests.RequestException as error:
            raise ValueError(
                f"Signed upload failed; retry cloud push ({type(error).__name__})"
            ) from error
        msgr.message(
            f"Input {draft.input_id}: upload complete — "
            f"{archive.size_bytes:,}/{archive.size_bytes:,} bytes (100%)"
        )
        draft = _record_upload(draft, "uploaded", instruction.transfer_id)
        transfer_id = instruction.transfer_id

    msgr.message(f"Input {draft.input_id}: confirming upload…")
    response = requests.post(
        f"{input_url}/confirm",
        headers=headers,
        json={
            "transfer_id": transfer_id,
            "size_bytes": archive.size_bytes,
            "sha256": archive.sha256,
        },
        timeout=30,
    )
    if response.status_code == 409:
        state = _confirmed_on_server(draft, session_token, archive)
        if state is not None:
            msgr.message(f"Input {draft.input_id}: upload already confirmed on server ({state})")
            return _record_upload(draft, "confirmed", transfer_id, state)
    response.raise_for_status()
    confirmation = InputConfirmationSchema.model_validate(response.json())
    if confirmation.input_id != draft.input_id:
        raise ValueError("Upload confirmation refers to another Input")
    msgr.message(f"Input {draft.input_id}: upload confirmed (server state: {confirmation.state})")
    return _record_upload(draft, "confirmed", transfer_id, confirmation.state)


def wait_for_input(draft: EnsembleDraft, session_token: str) -> EnsembleDraft:
    """Wait briefly for acceptance before creating Simulations."""
    if draft.input_id is None or draft.upload_stage != "confirmed":
        raise ValueError("Confirm the Input before checking validation")
    input_url = urls.get_input_endpoint(draft.input_id)
    msgr.message(f"Input {draft.input_id}: waiting for server validation…")
    started = time.monotonic()
    deadline = started + VALIDATION_WAIT_SECONDS
    last_report = started
    last_state: str | None = None
    while True:
        response = requests.get(
            input_url,
            headers={"X-Session-Token": session_token},
            timeout=30,
        )
        response.raise_for_status()
        status = InputUploadStatusSchema.model_validate(response.json())
        if status.input_id != draft.input_id:
            raise ValueError("Input status refers to another Input")
        if status.state in ("accepted", "failed") and (
            status.upload_confirmation is None
            or status.upload_confirmation.sha256 != draft.archive_sha256
        ):
            raise ValueError(f"Input {draft.input_id} was confirmed with a different archive")
        if status.state == "failed":
            failure = status.failure
            detail = (
                f"{failure.message}; next action: {failure.next_action}"
                if failure is not None
                else "inspect the Input failure with cloud status"
            )
            raise ValueError(
                f"Input {draft.input_id} rejected: {detail}. "
                "A rejected initial upload needs a new Ensemble (change the YAML, or change "
                "the archive and use --force)."
            )
        if status.state == "accepted":
            if status.members is None:
                raise ValueError(f"Accepted Input {draft.input_id} has no member mapping")
            actual = [(member.index, member.label) for member in status.members]
            expected = list(enumerate(draft.member_labels))
            if actual != expected:
                raise ValueError(f"Input {draft.input_id} member mapping differs from local order")
            msgr.message(f"Input {draft.input_id}: accepted, {len(status.members)} members")
            return draft
        now = time.monotonic()
        if status.state != last_state or now - last_report >= PROGRESS_REPORT_SECONDS:
            msgr.message(f"Input {draft.input_id}: {status.state} — {now - started:.0f} s elapsed")
            last_report = now
            last_state = status.state
        if now >= deadline:
            raise TimeoutError(
                f"Input {draft.input_id} is {status.state}; validation still pending. "
                "Retry cloud push to resume."
            )
        time.sleep(min(VALIDATION_POLL_SECONDS, max(0, deadline - time.monotonic())))


def _output_configuration(archive: BuiltArchive) -> dict[str, str | int | list[str] | None]:
    member = archive.ensemble.simulations[0]
    codes = list(member.outputs.raster_variables) or ["water_depth"]
    span = member.time.duration
    start = member.time.source_start or member.time.start
    absolute_start = None
    if member.time.temporal_type == TemporalType.ABSOLUTE:
        if start is None:
            raise ValueError("Absolute YAML time requires a start timestamp")
        absolute_start = start.astimezone(UTC).isoformat()

    return {
        "selected_output_codes": codes,
        "temporal_type": str(member.time.temporal_type),
        "absolute_start_time": absolute_start,
        "start_offset_us": 0,
        "end_offset_us": span // timedelta(microseconds=1),
        "report_interval_us": member.time.record_step // timedelta(microseconds=1),
    }


def _request_run(
    simulation_id: str, ensemble_id: str, headers: dict[str, str]
) -> RunResponseSchema:
    """Retry a Run request while its result repository is being sealed."""
    started = time.monotonic()
    deadline = started + RESULT_REPOSITORY_WAIT_SECONDS
    last_report = started - PROGRESS_REPORT_SECONDS
    while True:
        response = requests.post(
            urls.get_runs_endpoint(simulation_id),
            headers=headers,
            timeout=max(0.001, deadline - time.monotonic()),
        )
        if response.status_code == 409:
            failure = None
            try:
                failure = RequestErrorResponseSchema.model_validate(response.json())
            except ValueError:
                pass
            if (
                failure is not None
                and failure.code == "result_repository_not_sealed"
                and failure.next_action == "wait_for_result_repository"
            ):
                now = time.monotonic()
                if now - last_report >= PROGRESS_REPORT_SECONDS:
                    msgr.message(
                        f"Ensemble {ensemble_id}: waiting for result repository before requesting "
                        f"Run for Simulation {simulation_id} — {now - started:.0f} s elapsed"
                    )
                    last_report = now
                remaining = deadline - now
                if remaining > 0:
                    time.sleep(min(RESULT_REPOSITORY_POLL_SECONDS, remaining))
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Ensemble {ensemble_id} result repository is still not sealed after "
                        f"{RESULT_REPOSITORY_WAIT_SECONDS} seconds; "
                        "next action: wait_for_result_repository"
                    )
                continue
        response.raise_for_status()
        return RunResponseSchema.model_validate(response.json())


def launch_runs(archive: BuiltArchive, draft: EnsembleDraft, session_token: str) -> EnsembleDraft:
    """PUT immutable member configurations and request one idempotent Run per member."""
    if draft.input_id is None or draft.ensemble_id is None:
        raise ValueError("Create the Ensemble before creating Simulations")
    ensemble_id = draft.ensemble_id
    input_url = urls.get_input_endpoint(draft.input_id)
    output = _output_configuration(archive)
    sources = source_names(archive.simulations)
    headers = {"X-Session-Token": session_token}
    errors: list[str] = []
    total = len(archive.simulations)
    msgr.message(
        f"Ensemble {ensemble_id}: requesting runs — {len(draft.run_ids)}/{total} already requested"
    )
    for index, simulation in enumerate(archive.simulations):
        member = f"member {index} ({draft.member_labels[index]})"
        stage = "creating Simulation"
        try:
            simulation_id = draft.simulation_ids.get(index)
            if simulation_id is None:
                msgr.message(f"Ensemble {ensemble_id}: {member}, {stage}…")
                config = simulation.simulation_config
                kinds = dict(simulation.input_kinds)
                response = requests.put(
                    f"{input_url}/simulations/{index}",
                    headers=headers,
                    json={
                        "output_configuration": output,
                        "configuration": {
                            "input_map_names": {
                                role: sources[(name, kinds[role])]
                                for role, name in config.input_map_names.items()
                            },
                            "surface_flow_parameters": config.surface_flow_parameters.model_dump(),
                            "dtinf": config.dtinf,
                            "infiltration_model": str(config.infiltration_model),
                        },
                    },
                    timeout=30,
                )
                response.raise_for_status()
                created = SimulationResponseSchema.model_validate(response.json())
                if (
                    created.input_id != draft.input_id
                    or created.ensemble_id != draft.ensemble_id
                    or created.member_index != index
                    or created.member_label != draft.member_labels[index]
                ):
                    raise ValueError("Simulation response does not match the accepted member")
                simulation_id = created.simulation_id
                draft = draft.model_copy(
                    update={"simulation_ids": draft.simulation_ids | {index: simulation_id}}
                )
                save_ensemble_draft(draft)
            else:
                msgr.message(
                    f"Ensemble {ensemble_id}: {member}, reusing Simulation {simulation_id}"
                )
            if index not in draft.run_ids:
                stage = "requesting Run"
                msgr.message(f"Ensemble {ensemble_id}: {member}, {stage}…")
                run = _request_run(
                    simulation_id,
                    ensemble_id,
                    {
                        **headers,
                        "Idempotency-Key": hashlib.sha256(
                            f"{draft.idempotency_key}/run/{index}".encode()
                        ).hexdigest(),
                    },
                )
                if run.simulation_id != simulation_id:
                    raise ValueError("Run response refers to another Simulation")
                draft = draft.model_copy(update={"run_ids": draft.run_ids | {index: run.run_id}})
                save_ensemble_draft(draft)
                msgr.message(
                    f"Ensemble {ensemble_id}: {member}, Run {run.run_id} requested — "
                    f"{len(draft.run_ids)}/{total} runs requested"
                )
            else:
                msgr.message(
                    f"Ensemble {ensemble_id}: {member}, "
                    f"Run {draft.run_ids[index]} already requested"
                )
        except (requests.RequestException, ValueError, KeyError, TimeoutError) as error:
            if isinstance(error, requests.HTTPError) and error.response is not None:
                request = error.response.request
                print(f"{request.method} {request.url}: {request.body}", file=sys.stderr)
                print(
                    f"member {index} server response ({error.response.status_code}): "
                    f"{error.response.text}",
                    file=sys.stderr,
                )
            detail = f"{member}, {stage} failed: {error}"
            msgr.warning(f"Ensemble {ensemble_id}: {detail}")
            errors.append(detail)
    if errors:
        raise ValueError(
            f"Ensemble {draft.ensemble_id} launched "
            f"{len(draft.run_ids)}/{len(archive.simulations)} members; "
            f"{'; '.join(errors)}. Retry cloud push to resume."
        )
    return draft
