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
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal

import requests

from itzi.cloud import urls
from itzi.cloud.archive import BuiltArchive
from itzi.cloud.metadata_storage import (
    EnsembleDraft,
    get_or_create_ensemble_draft,
    save_ensemble_draft,
)
from itzi.cloud.schemas import (
    EnsembleInputResponseSchema,
    InputConfirmationSchema,
    InputUploadInstructionSchema,
    InputUploadStatusSchema,
)


def create_ensemble(
    archive: BuiltArchive, *, project_id: str, email: str, session_token: str
) -> EnsembleDraft:
    """Create once, replay an interrupted request, or resume its recorded IDs."""
    source = archive.ensemble.source
    grass = archive.simulations[0].grass_params
    draft = get_or_create_ensemble_draft(
        email,
        project_id,
        source.path,
        source.document_index,
        tuple(sim.simulation_id for sim in archive.simulations),
        replace(grass, region=None, mask=None),
        archive.sha256,
    )
    if draft.ensemble_id is not None and draft.input_id is not None:
        return draft

    response = requests.post(
        urls.get_ensembles_endpoint(project_id),
        headers={"X-Session-Token": session_token, "Idempotency-Key": draft.idempotency_key},
        timeout=30,
    )
    if response.status_code == 409:
        raise ValueError(f"Ensemble creation conflict: {response.json()['detail']}")
    response.raise_for_status()
    created = EnsembleInputResponseSchema.model_validate(response.json())
    draft = draft.model_copy(
        update={"ensemble_id": created.ensemble_id, "input_id": created.input_id}
    )
    save_ensemble_draft(draft)
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
        return draft

    input_url = urls.get_input_endpoint(draft.input_id)
    headers = {"X-Session-Token": session_token}
    if draft.upload_stage is not None:
        state = _confirmed_on_server(draft, session_token, archive)
        if state is not None:
            return _record_upload(draft, "confirmed", draft.transfer_id or "", state)

    if draft.upload_stage == "uploaded":
        if draft.transfer_id is None:
            raise ValueError("Uploaded Input has no saved transfer ID")
        transfer_id = draft.transfer_id
    else:
        md5 = hashlib.md5()  # API checksum contract
        sha256 = hashlib.sha256()
        with archive.path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                md5.update(chunk)
                sha256.update(chunk)
        if sha256.hexdigest() != archive.sha256:
            raise ValueError("Archive SHA-256 changed since building the Input")
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
        try:
            with archive.path.open("rb") as file:
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
        draft = _record_upload(draft, "uploaded", instruction.transfer_id)
        transfer_id = instruction.transfer_id

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
            return _record_upload(draft, "confirmed", transfer_id, state)
    response.raise_for_status()
    confirmation = InputConfirmationSchema.model_validate(response.json())
    if confirmation.input_id != draft.input_id:
        raise ValueError("Upload confirmation refers to another Input")
    return _record_upload(draft, "confirmed", transfer_id, confirmation.state)
