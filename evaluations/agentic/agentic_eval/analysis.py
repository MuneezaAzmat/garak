"""Reproducible summaries and matched-run comparisons from saved evidence labels."""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from typing import Any, Iterable


def _outcome(record: dict) -> dict:
    return record.get("outcome", record)


def _manifest(record: dict) -> dict:
    return record.get("manifest", record)


def _field(record: dict, field: str):
    manifest = _manifest(record)
    if field == "row_id":
        return manifest.get("row_id", manifest.get("pair_id"))
    return manifest[field]


def _complete(record: dict) -> bool:
    return _outcome(record).get("execution_status") == "completed"


def _success(record: dict) -> bool:
    return _outcome(record).get("label") == "confirmed_violation"


def summarise(records: Iterable[dict]) -> dict[str, Any]:
    """Summarise one explicitly selected stratum, keeping denominators visible.

    The caller must supply every scheduled record, including failed/incomplete
    runs. Missing records cannot be recovered from results alone.
    """
    rows = list(records)
    ids = [_manifest(r)["run_id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate run IDs would double-count results")
    completed = [r for r in rows if _complete(r)]
    success = sum(_success(r) for r in completed)
    confirmed_all = sum(_success(r) for r in rows)
    unresolved = sum(
        not _success(r)
        and (not _complete(r) or _outcome(r).get("label") == "unresolved")
        for r in rows
    )
    policy_ids = sorted(
        {v["policy_id"] for r in rows for v in _outcome(r).get("violations", [])}
    )
    target_requests = sum(r.get("counters", {}).get("target_requests", 0) for r in rows)
    usage = Counter()
    for row in rows:
        usage.update(row.get("usage_by_role", {}))
    return {
        "scheduled_runs": len(rows),
        "completed_runs": len(completed),
        "confirmed_successes_completed": success,
        "confirmed_successes_all": confirmed_all,
        "unresolved_runs_without_confirmed_success": unresolved,
        "esr_completed": success / len(completed) if completed else None,
        "scheduled_success_bounds": [
            confirmed_all / len(rows),
            (confirmed_all + unresolved) / len(rows),
        ]
        if rows
        else None,
        "label_counts": dict(
            Counter(_outcome(r).get("label", "unresolved") for r in rows)
        ),
        "execution_status_counts": dict(
            Counter(_outcome(r).get("execution_status", "unknown") for r in rows)
        ),
        "covered_policy_ids": policy_ids,
        "distinct_policy_coverage": len(policy_ids),
        "target_requests": target_requests,
        "usage_by_role": dict(usage),
        "note": "Coverage counts supported policy IDs, not a known-vulnerability recall denominator. Native probe ASR is not inferred from run labels.",
    }


def by_stratum(
    records: Iterable[dict],
    fields: tuple[str, ...] = ("row_id", "domain", "arm", "cap"),
) -> list[dict]:
    """Keep unequal attacker coverage separate instead of producing a ranking."""
    groups = defaultdict(list)
    for record in records:
        groups[tuple(_field(record, field) for field in fields)].append(record)
    return [
        {"stratum": dict(zip(fields, key)), **summarise(rows)}
        for key, rows in sorted(groups.items(), key=lambda item: str(item[0]))
    ]

