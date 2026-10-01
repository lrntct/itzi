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

from dataclasses import replace

import requests

from itzi.cloud import urls
from itzi.cloud.archive import BuiltArchive
from itzi.cloud.metadata_storage import (
    EnsembleDraft,
    get_or_create_ensemble_draft,
    save_ensemble_draft,
)
from itzi.cloud.schemas import EnsembleInputResponseSchema


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
