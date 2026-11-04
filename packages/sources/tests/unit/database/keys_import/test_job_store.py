# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Keys import: job records (work/add-bulk-import.txt, section 3.6, J1-J3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from coa_sources.database.keys_import.job_store import JOB_ID_RE, JobStore, new_job_id, plain

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 4, 15, 30, 12, tzinfo=UTC)


class _PagedTable:
    """Returns the given query pages in order and records each call."""

    def __init__(self, pages: list[dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.pages[len(self.calls) - 1]


def test_j1_job_id_has_start_time_and_random_suffix() -> None:
    job_id = new_job_id(NOW)
    assert job_id.startswith("20261104T153012Z-")
    assert JOB_ID_RE.match(job_id)
    assert new_job_id(NOW) != job_id


def test_j2_running_job_found_on_a_later_page() -> None:
    running = {
        "SK": "20261104T150000Z-00000002",
        "status": "IN_PROGRESS",
        "startedAt": (NOW - timedelta(minutes=5)).isoformat(),
    }
    table = _PagedTable([{"Items": [], "LastEvaluatedKey": {"PK": "SRC#s", "SK": "a"}}, {"Items": [running]}])
    assert JobStore(table).find_running("s", NOW)["SK"] == running["SK"]
    assert table.calls[1]["ExclusiveStartKey"] == {"PK": "SRC#s", "SK": "a"}


def test_j2_only_stale_running_jobs_means_none_running() -> None:
    stale = {
        "SK": "20261101T100000Z-00000001",
        "status": "IN_PROGRESS",
        "startedAt": (NOW - timedelta(hours=3)).isoformat(),
    }
    assert JobStore(_PagedTable([{"Items": [stale]}])).find_running("s", NOW) is None


def test_j3_decimals_from_dynamodb_become_plain_numbers() -> None:
    assert plain({"a": Decimal("2"), "b": [Decimal("1.5")], "c": "x"}) == {"a": 2, "b": [1.5], "c": "x"}
