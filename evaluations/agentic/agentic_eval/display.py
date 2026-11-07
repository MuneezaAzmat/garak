"""Small Markdown views of evaluation configurations and recorded progress."""

from collections import Counter
from datetime import datetime
from functools import lru_cache
import json
from pathlib import Path
import tempfile
import time
from zoneinfo import ZoneInfo


SCENARIOS = {
    "klarna": (
        "MiniKlarna · customer service",
        "Orders, accounts, refunds, payments, policies, escalation",
    ),
    "airbnb": (
        "MiniAirbnb · travel support",
        "Reservations, listings, availability, policies, moderation, escalation",
    ),
    "occiai": (
        "MiniOcciAI · patient support",
        "Patient records, referrals, education, questionnaires, EHR, escalation",
    ),
}
PROBES = {
    "agent-breaker": "AgentBreaker",
    "toolchain": "ToolChain",
    "agent-breaker-toolchain": "Combined (historical)",
}


def eval_id(manifest, index):
    """Return a short row reference within its matrix, distinct from model-pair IDs."""
    return f"{manifest['stage'][0].upper()}{index:03d}"


def cell(value):
    """Keep model names, errors and tool labels inside one Markdown cell."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def write_text(path, text):
    """Publish a complete view while another process may be reading it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, suffix=".tmp", delete=False
    ) as stream:
        stream.write(text)
        temporary = Path(stream.name)
    temporary.replace(path)


def matrix_markdown(matrix):
    """Render every configuration without low-level inference settings."""
    show_repeat = len({row["block_id"] for row in matrix}) > 1
    lines = [
        "# Planned main-study matrix"
        if matrix[0]["stage"] == "main"
        else "# Pilot evaluation matrix",
        "",
        f"{len(matrix):,} evaluations. IDs identify rows in this matrix; P01–P08 in JSON identify model pairs.",
        "",
        "All agents use unsafe backends and built-in data. Tool scope lists available tool families, not proof that each tool was exercised. Budget means target requests, not planning or judge calls.",
        "",
        "| ID | Probe | Target | Attacker | Judge | Scenario | Tools in scope | Budget |"
        + (" Repeat |" if show_repeat else ""),
        "|---|---|---|---|---|---|---|---:|" + ("---:|" if show_repeat else ""),
    ]
    for index, row in enumerate(matrix, 1):
        scenario, tools = SCENARIOS[row["domain"]]
        names = [
            row["requested_models"][role]["name"]
            for role in ("target", "attacker", "judge")
        ]
        repeat = int(row["block_id"].rsplit("-", 1)[1]) + 1
        values = [
            eval_id(row, index),
            PROBES[row["arm"]],
            *names,
            scenario,
            tools,
            row["cap"],
            repeat,
        ]
        if not show_repeat:
            values.pop()
        lines.append("| " + " | ".join(cell(v) for v in values) + " |")
    if any(row["arm"] == "agent-breaker-toolchain" for row in matrix):
        lines.insert(
            2,
            "Historical matrix: the combined arm is excluded from the current study.\n",
        )
    return "\n".join(lines) + "\n"


def _read(path):
    return json.loads(path.read_text()) if path.exists() else None


def _events(path):
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_bytes().splitlines(keepends=True)
        if line.strip() and line.endswith(b"\n")
    ]


@lru_cache(maxsize=2048)
def _finished_tools(path, modified, size):
    """Cache only tool names, avoiding repeated reads of completed model transcripts."""
    return tuple(
        sorted(
            {
                e.get("payload", {}).get("tool_name", "")
                for e in _events(path)
                if e.get("event_type") == "tool_call"
            }
            - {""}
        )
    )


def _comment(run, events, result, active, paused):
    if result:
        outcome = result["outcome"]
        status = outcome["execution_status"]
        violations = sorted({v["policy_id"] for v in outcome.get("violations", [])})
        detail = (
            "Confirmed: " + ", ".join(violations)
            if violations
            else "No confirmed violation."
        )
        if status not in ("completed", "no_candidates"):
            return status.replace("_", " ").capitalize() + "; " + detail
        return (
            "No executable attacks generated." if status == "no_candidates" else detail
        )
    if paused:
        return "Paused; recorded requests will not be replayed."
    if not active:
        return "Waiting to run."
    activity = _read(run / "activity.json")
    if activity and activity["state"] in ("waiting", "retrying"):
        seconds = max(0, int(time.time() - activity["updated_at"]))
        duration = f"{seconds // 60}m {seconds % 60}s"
        role = {
            "attacker": "attacker/helper",
            "judge": "judge",
            "target": "target agent",
        }[activity["role"]]
        action = "Retrying" if activity["state"] == "retrying" else "Waiting for"
        return f"{action} {role} response ({duration})."
    if not events:
        return "Starting the probe; no response recorded yet."
    latest = events[-1]
    descriptions = {
        "request_outcome": "target response assessed",
        "target_request": "target request submitted",
        "target_response": "target response received",
        "judge_verdict": "judge verdict received",
        "native_attempt": "attack prepared",
        "role_model_call": f"{latest.get('role', 'model')} call returned",
    }
    return (
        "Last recorded: "
        + descriptions.get(latest.get("event_type"), "probe activity")
        + "."
    )


def write_views(directory, matrix, progress=None):
    """Write linked configuration and progress tables, including unstarted rows."""
    directory = Path(directory)
    write_text(directory / "matrix.md", matrix_markdown(matrix))
    batch = _read(directory / "batch-status.json") or {}
    progress = progress or _read(directory / "progress.json") or {}
    current = batch.get("run_id", progress.get("run_id"))
    state = batch.get("status", progress.get("status", "Not started"))
    selected = batch.get("selected_run_ids")
    if selected is None and current and batch.get("selected_runs") == 1:
        selected = [current]
    rows, counts = [], Counter()
    active_id = None
    for index, manifest in enumerate(matrix, 1):
        ident = eval_id(manifest, index)
        run = directory / "runs" / manifest["run_id"]
        result = _read(run / "result.json")
        event_path = run / "events.jsonl"
        events = [] if result else _events(event_path)
        active = (
            manifest["run_id"] == current
            and not result
            and state in ("Running", "Checking saved result")
        )
        paused = (
            manifest["run_id"] == current and not result and state.startswith("Paused")
        )
        if result:
            execution = result["outcome"]["execution_status"]
            status = {"completed": "Done", "no_candidates": "No candidates"}.get(
                execution, "Failed"
            )
        elif paused:
            status = "Paused"
        elif active:
            status, active_id = "▶ Running", ident
        elif selected is not None and manifest["run_id"] not in selected:
            status = "Not selected"
        elif (run / "started.json").exists():
            status = "Unfinished"
        else:
            status = "Pending"
        counts[status] += 1
        requests = (
            result.get("counters", {}).get("target_requests", 0)
            if result
            else sum(e.get("event_type") == "target_request" for e in events)
        )
        tools = sorted(
            {
                e.get("payload", {}).get("tool_name", "")
                for e in events
                if e.get("event_type") == "tool_call"
            }
            - {""}
        )
        if result and event_path.exists():
            stat = event_path.stat()
            tools = _finished_tools(event_path, stat.st_mtime_ns, stat.st_size)
        comment = (
            "Outside this selected batch."
            if status == "Not selected"
            else _comment(run, events, result, active, paused)
        )
        link = f"[{ident}](runs/{manifest['run_id']}/result.json)" if result else ident
        if active:
            link = f"**{link}**"
        values = [
            link,
            PROBES[manifest["arm"]],
            SCENARIOS[manifest["domain"]][0].split(" · ")[0],
            status,
            f"{requests}/{manifest['cap']}",
            ", ".join(tools) or "—",
            comment,
        ]
        rows.append("| " + " | ".join(cell(v) for v in values) + " |")
    finished = counts["Done"] + counts["Failed"] + counts["No candidates"]
    selected_count = len(selected) if selected is not None else len(matrix)
    updated = datetime.now(ZoneInfo("America/Los_Angeles")).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )
    links = "[Configuration matrix](matrix.md)"
    if (directory / "progress.json").exists():
        links += " · [Detailed JSON](progress.json)"
    lines = [
        "# Evaluation progress",
        "",
        f"**{state}.** Active row: **{active_id or 'none'}**. {finished}/{len(matrix)} matrix rows finished; {selected_count} selected. Done: {counts['Done']}; failed: {counts['Failed']}; no candidates: {counts['No candidates']}.",
        "",
        links + f" · Updated {updated}",
        "",
        "Requests show budget used, not percentage complete: planning and judging also take time, and a run can finish below its cap. Comments use recorded activity; older logs may only show the last completed event.",
        "",
        "| ID | Probe | Scenario | Status | Requests | Tools called | Comment |",
        "|---|---|---|---|---:|---|---|",
        *rows,
    ]
    if any(row["arm"] == "agent-breaker-toolchain" for row in matrix):
        lines.insert(
            2,
            "Historical batch: the combined arm is excluded from the current study.\n",
        )
    write_text(directory / "progress.md", "\n".join(lines) + "\n")
