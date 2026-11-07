"""Refresh progress.json from live artifacts without changing the experiment.

Run alongside an existing pilot with --watch, or omit it for one snapshot.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic_eval.display import eval_id, write_views


def read_json(path):
    """Read an atomically published artifact when it exists."""
    return json.loads(path.read_text()) if path.exists() else None


def events_at(path):
    """Ignore only an unfinished final JSONL line while the runner appends."""
    if not path.exists():
        return []
    lines = path.read_bytes().splitlines(keepends=True)
    return [json.loads(line) for line in lines if line.strip() and line.endswith(b"\n")]


def timestamp(value):
    """Render machine timestamps in explicitly labelled UTC."""
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


def refresh(directory):
    """Derive current activity and results from the actual saved run artifacts."""
    now = time.time()
    matrix = read_json(directory / "matrix.json")
    previous = read_json(directory / "progress.json") or {}
    batch = read_json(directory / "batch-status.json")
    records = []
    summaries = []
    for index, manifest in enumerate(matrix, 1):
        run = directory / "runs" / manifest["run_id"]
        started = read_json(run / "started.json")
        result = read_json(run / "result.json")
        if started:
            records.append((started["started_at"], index, manifest, run, result))
        elif batch and batch["run_id"] == manifest["run_id"]:
            records.append((batch["updated_at"], index, manifest, run, result))
        if result:
            outcome = result["outcome"]
            summaries.append(
                {
                    "run": index,
                    "row_id": manifest["row_id"],
                    "domain": manifest["domain"],
                    "arm": manifest["arm"],
                    "execution_status": outcome["execution_status"],
                    "outcome": outcome["label"],
                    "target_requests_used": result["counters"]["target_requests"],
                    "request_budget": manifest["cap"],
                    "first_success_request": result.get("first_success_request"),
                    "violated_policies": sorted(
                        {v["policy_id"] for v in outcome.get("violations", [])}
                    ),
                    "violation_details": list(
                        dict.fromkeys(
                            v["detail"] for v in outcome.get("violations", [])
                        )
                    ),
                    "elapsed_seconds": round(
                        (run / "result.json").stat().st_mtime - started["started_at"]
                    )
                    if started
                    else None,
                }
            )
    if not records:
        write_views(directory, matrix, previous)
        return None
    started_at, index, manifest, run, result = max(records, key=lambda row: row[0])
    if batch:
        started_at, index, manifest, run, result = next(
            row for row in records if row[2]["run_id"] == batch["run_id"]
        )
    events = events_at(run / "events.jsonl")
    counts = Counter(e.get("event_type") for e in events)
    requests = [e for e in events if e.get("event_type") == "target_request"]
    outcomes = [
        e["payload"] for e in events if e.get("event_type") == "request_outcome"
    ]
    violations = {
        (v["policy_id"], v["tool_name"]): {
            k: v[k] for k in ("policy_id", "tool_name", "detail")
        }
        for outcome in outcomes
        for v in outcome.get("violations", [])
    }
    latest = events[-1] if events else {}
    judges = [e for e in events if e.get("event_type") == "judge_verdict"]
    errors = [
        e
        for e in events
        if e.get("event_type") == "run_error" or e.get("status") == "provider_failure"
    ]
    state = "Running"
    if result:
        execution = result["outcome"]["execution_status"]
        state = (
            "Finished: " if execution in ("completed", "no_candidates") else "Paused: "
        ) + execution
    elif previous.get("run_id") == manifest["run_id"] and previous.get(
        "status", ""
    ).startswith("Paused"):
        state = previous["status"]
    last_recorded_at = (run / "events.jsonl").stat().st_mtime if events else started_at
    if batch:
        state = batch["status"]
    finish_at = (run / "result.json").stat().st_mtime if result else now
    progress = {
        "updated_at": now,
        "updated_at_utc": timestamp(now),
        "status": state,
        "processed_runs": len(summaries),
        "completed_runs": sum(s["execution_status"] == "completed" for s in summaries),
        "no_candidate_runs": sum(
            s["execution_status"] == "no_candidates" for s in summaries
        ),
        "failed_runs": sum(
            s["execution_status"] not in ("completed", "no_candidates")
            for s in summaries
        ),
        "selected_runs": (batch or previous).get("selected_runs", len(matrix)),
        "scheduled_runs": len(matrix),
        "current_run": index,
        "evaluation_id": eval_id(manifest, index),
        "run_id": manifest["run_id"],
        "row_id": manifest["row_id"],
        "domain": manifest["domain"],
        "arm": manifest["arm"],
        "models": {
            role: manifest["requested_models"][role]["name"]
            for role in ("target", "attacker", "judge")
        },
        "current_run_details": {
            "elapsed_seconds": round(finish_at - started_at),
            "output_token_allowances": {
                role: manifest["limits"][role + "_output_tokens"]
                for role in ("target", "attacker", "judge")
            },
            "request_timeout_seconds": manifest["limits"]["request_timeout"],
            "provider_retry_attempts": sum(
                e.get("event_type") == "role_model_call"
                and e.get("attempt_number", 1) > 1
                for e in events
            ),
            "target_requests_used": len(requests),
            "request_budget": manifest["cap"],
            "requests_by_component": dict(
                Counter(e.get("component") for e in requests)
            ),
            "component_budgets": manifest["component_caps"],
            "target_responses_received": counts["target_response"],
            "tool_calls": counts["tool_call"],
            "judge_verdicts_received": len(judges),
            "recorded_model_calls_by_role": dict(
                Counter(
                    e.get("role")
                    for e in events
                    if e.get("event_type") in ("role_model_call", "model_call")
                )
            ),
            "token_limited_responses": sum(
                e.get("event_type") == "target_response"
                and e.get("status") == "incomplete"
                for e in events
            ),
            "request_outcomes": dict(Counter(o["label"] for o in outcomes)),
            "confirmed_violations": list(violations.values()),
            "latest_recorded_event": latest.get(
                "event_type", "waiting_for_first_recorded_response"
            ),
            "latest_event_role": latest.get("role"),
            "seconds_since_last_record": round(now - last_recorded_at),
            "latest_judge_verdict": {
                "kind": judges[-1].get("judgment_kind"),
                "verdict": judges[-1].get("verdict"),
            }
            if judges
            else None,
            "error": (
                errors[-1]["payload"]
                if errors[-1].get("event_type") == "run_error"
                else {
                    "role": errors[-1].get("role"),
                    "model": errors[-1].get("model"),
                    "type": errors[-1].get("error"),
                    "message": errors[-1].get(
                        "error_message",
                        "Provider call failed; the original trace did not retain a more specific error message.",
                    ),
                }
            )
            if errors
            else None,
        },
        "finished_run_summaries": summaries,
        "monitor_note": "Refreshed every 3 seconds while watching. Model calls appear after they return; latest event is an observation, not proof of what is currently in flight. No-candidate runs are shown separately from completed evaluations.",
    }
    # A unique temporary name avoids collision with the running pilot writer.
    with tempfile.NamedTemporaryFile(
        mode="w", dir=directory, prefix=".progress-", suffix=".tmp", delete=False
    ) as stream:
        json.dump(progress, stream, indent=2)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(directory / "progress.json")
    write_views(directory, matrix, progress)
    return progress


def main():
    """Watch an existing pilot without making model requests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    while True:
        progress = refresh(args.output.resolve())
        if not args.watch or (
            progress
            and (
                progress["status"].startswith("Paused")
                or progress["status"].startswith("Batch finished")
                or progress["processed_runs"] == progress["scheduled_runs"]
            )
        ):
            break
        time.sleep(3)


if __name__ == "__main__":
    main()
