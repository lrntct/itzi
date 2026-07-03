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
from datetime import datetime
import json

import itzi.messenger as msgr
from itzi.cloud import urls
from itzi.cloud.schemas import SimulationTaskSchema

try:
    import requests
except ImportError:
    raise ImportError(
        "To use the cloud functionalities, install itzi with: "
        "'uv tool install itzi[cloud]' "
        "or 'pip install itzi[cloud]'"
    )


def get_simulations_list(session_token: str, url: str | None = None) -> list[SimulationTaskSchema]:
    """Get the status of all simulations for the authenticated user.

    Args:
        session_token: Authentication session token
        url: API endpoint URL for status retrieval

    Returns:
        List of SimulationTaskSchema objects containing simulation status
    """
    url = url or urls.get_simulations_endpoint()
    headers = {"X-Session-Token": session_token}

    with requests.Session() as session:
        response = session.get(url, headers=headers)

        if response.status_code != 200:
            msgr.fatal(
                f"Failed to retrieve simulation status. "
                f"Code: {response.status_code}. Reason: {response.reason}"
            )

        response_data = json.loads(response.text)

    # Parse the response and create SimulationTaskSchema objects
    # The API returns a list of tasks directly
    tasks = []
    for task_data in response_data:
        task = SimulationTaskSchema(
            team=task_data.get("team", ""),
            created_on=datetime.fromisoformat(task_data["created_on"]),
            last_updated=datetime.fromisoformat(task_data["last_updated"]),
            fingerprint=task_data["fingerprint"],
            status=task_data["status"],
            progress=task_data["progress"],
            input_bytes=task_data["input_bytes"],
            results_bytes=task_data["results_bytes"],
        )
        tasks.append(task)

    return tasks


def get_simulation(
    session_token: str, fingerprint: str, url: str | None = None
) -> SimulationTaskSchema:
    """Get the status of a single simulation by fingerprint.

    Args:
        session_token: Authentication session token
        fingerprint: Simulation fingerprint to query
        url: Base API endpoint URL for simulations

    Returns:
        SimulationTaskSchema object containing simulation status
    """
    url = url or urls.get_simulations_endpoint()
    headers = {"X-Session-Token": session_token}
    simulation_url = f"{url}/{fingerprint}"

    with requests.Session() as session:
        response = session.get(simulation_url, headers=headers)

        if response.status_code != 200:
            msgr.fatal(
                f"Failed to retrieve simulation status. "
                f"Code: {response.status_code}. Reason: {response.reason}"
            )

        response_data = json.loads(response.text)

    # Parse the response and create SimulationTaskSchema object
    # The API returns a single task object
    task = SimulationTaskSchema(
        team=response_data.get("team", ""),
        created_on=datetime.fromisoformat(response_data["created_on"]),
        last_updated=datetime.fromisoformat(response_data["last_updated"]),
        fingerprint=response_data["fingerprint"],
        status=response_data["status"],
        progress=response_data["progress"],
        input_bytes=response_data["input_bytes"],
        results_bytes=response_data["results_bytes"],
    )

    return task


def display_simulations_list(tasks: list[SimulationTaskSchema]) -> None:
    """Display simulation status in a table format similar to docker ps.

    Args:
        tasks: List of simulation tasks to display
    """
    if not tasks:
        msgr.message("No simulations found.")
        return

    # Helper function to format bytes
    def format_bytes(bytes_val: float) -> str:
        if bytes_val == 0:
            return "0B"
        for unit in ["B", "KB", "MB", "GB"]:
            if bytes_val < 1024.0:
                return f"{bytes_val:.1f}{unit}"
            bytes_val = bytes_val / 1024.0
        return f"{bytes_val:.1f}TB"

    # Display timestamps in the user's local timezone.
    def format_local_datetime(dt: datetime) -> str:
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")

    # Print header
    header = f"{'FINGERPRINT':<17} {'STATUS':<12} {'PROGRESS':<10} {'CREATED':<20} {'UPDATED':<20} {'TEAM':<15} {'INPUT SIZE':<12} {'RESULTS SIZE':<10}"
    msgr.message(header)

    # Print each task
    for task in tasks:
        fingerprint = task.fingerprint
        status = task.status[:11]  # Truncate if needed
        progress = f"{task.progress / 10:.1f}%"  # received progress is in per mille
        created = format_local_datetime(task.created_on)
        updated = format_local_datetime(task.last_updated)
        team = task.team[:14] if task.team else "-"
        input_size = format_bytes(task.input_bytes)
        results_size = format_bytes(task.results_bytes)

        row = f"{fingerprint:<17} {status:<12} {progress:<10} {created:<20} {updated:<20} {team:<15} {input_size:<12} {results_size:<10}"
        msgr.message(row)
