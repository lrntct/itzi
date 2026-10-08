"""
Copyright (C) 2025-2026 Laurent Courty

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
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

import itzi.messenger as msgr
from itzi.cloud import urls
from itzi.cloud.metadata_storage import list_ensemble_drafts
from itzi.cloud.schemas import (
    PackageDownloadSchema,
    PackageRequestResponseSchema,
    PackageRequestSchema,
    PackageStatusResponseSchema,
    RequestErrorResponseSchema,
)

PACKAGE_WAIT_SECONDS = 60
PACKAGE_POLL_SECONDS = 2


def select_run_ids(email: str, ensemble_id: str, run_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Use explicit Run IDs, or the selected Ensemble's account-scoped local records."""
    if not run_ids:
        drafts = tuple(
            draft for draft in list_ensemble_drafts(email) if draft.ensemble_id == ensemble_id
        )
        run_ids = tuple(
            dict.fromkeys(run for draft in drafts for _, run in sorted(draft.run_ids.items()))
        )
        if not run_ids:
            raise ValueError(
                f"No recorded runs for Ensemble {ensemble_id} and {email}; "
                "specify 1–100 --run-id values or finish cloud push."
            )
    return PackageRequestSchema(run_ids=run_ids).run_ids


def _check_package_response(response: requests.Response) -> None:
    if not response.ok:
        try:
            error = RequestErrorResponseSchema.model_validate(response.json())
        except ValueError:
            response.raise_for_status()
        else:
            raise ValueError(
                f"Package request failed ({error.code}): {error.detail}; "
                f"next action: {error.next_action}"
            )


def wait_for_package(
    ensemble_id: str, package: PackageRequestResponseSchema, session_token: str
) -> PackageDownloadSchema:
    """Poll the frozen package, obtaining fresh signed instructions when ready."""
    deadline = time.monotonic() + PACKAGE_WAIT_SECONDS
    while True:
        response = requests.get(
            urls.get_package_endpoint(ensemble_id, package.package_id),
            headers={"X-Session-Token": session_token},
            timeout=30,
        )
        _check_package_response(response)
        status = PackageStatusResponseSchema.model_validate(response.json())
        if (
            status.package_id != package.package_id
            or status.manifest_sha256 != package.manifest_sha256
        ):
            raise ValueError("Package status does not match the requested package manifest")
        if status.state == "failed":
            raise ValueError(
                f"Package {package.package_id} failed ({status.failure_code or 'export failed'}); "
                "retry cloud pull to request a new package."
            )
        if status.state == "ready":
            download = status.download
            if download is None or download.artifact_expires_at <= datetime.now(UTC):
                raise ValueError(
                    f"Package {package.package_id} artifact is unavailable or expired; "
                    "retry cloud pull to request a new package."
                )
            return download
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Package {package.package_id} is still {status.state}; retry cloud pull later."
            )
        time.sleep(min(PACKAGE_POLL_SECONDS, max(0, deadline - time.monotonic())))


def download_package(download: PackageDownloadSchema, destination: Path) -> bool:
    """Stream and verify the archive, publishing it atomically without overwriting."""
    with requests.get(
        download.url, headers=download.headers, stream=True, timeout=300
    ) as response:
        if response.status_code in (401, 403):
            return False  # The signed URL may have expired in transit; refresh via the API.
        if response.status_code in (404, 410):
            raise ValueError("Package artifact is unavailable or expired; request a new package.")
        if response.status_code != 200:
            raise ValueError(f"Signed package download failed (HTTP {response.status_code})")
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".itzi-package-") as file:
            size = 0
            digest = hashlib.sha256()
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                size += len(chunk)
                if size > download.size_bytes:
                    raise ValueError("Result package size mismatch: download exceeds size_bytes")
                digest.update(chunk)
                file.write(chunk)
            if size != download.size_bytes:
                raise ValueError(
                    f"Result package size mismatch: expected {download.size_bytes}, "
                    f"received {size}"
                )
            if digest.hexdigest() != download.archive_sha256.lower():
                raise ValueError("Result package SHA-256 mismatch")
            file.flush()
            os.link(file.name, destination)
    return True


def pull_result_package(
    session_token: str,
    email: str,
    ensemble_id: str,
    run_ids: tuple[str, ...],
    destination: Path,
) -> Path:
    """Select Runs, request their package, and save only a verified archive."""
    if destination.exists():
        raise FileExistsError(f"Destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError(f"Destination directory does not exist: {destination.parent}")
    selected = select_run_ids(email, ensemble_id, run_ids)
    response = requests.post(
        urls.get_packages_endpoint(ensemble_id),
        headers={"X-Session-Token": session_token},
        json=PackageRequestSchema(run_ids=selected).model_dump(mode="json"),
        timeout=30,
    )
    _check_package_response(response)
    package = PackageRequestResponseSchema.model_validate(response.json())
    msgr.message(
        f"Ensemble {ensemble_id}: package {package.package_id} requested for {len(selected)} runs"
    )
    for _ in range(3):
        download = wait_for_package(ensemble_id, package, session_token)
        if download.url_expires_at <= datetime.now(UTC):
            continue
        if download_package(download, destination):
            return destination
    raise ValueError(
        f"Package {package.package_id} signed download URL repeatedly expired or was denied; "
        "retry cloud pull later."
    )
