"""Inventory, run and report the bounded Asago / actual MiniKlarna panel."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .asago_bridge import project_user_seed, coverage_rows
from .runner import worker_environment, read_jsonl
from .worker import _write_json

ROOT = Path(__file__).resolve().parents[2]


def inventory(panel, artifacts, run_dir):
    scenarios = json.loads(Path(panel).read_text())
    decisions = {}
    for scenario in scenarios:
        directory = Path(artifacts) / scenario["id"]
        artifact = directory / f'{scenario["id"]}-garak.json'
        validation = directory / "validation.json"
        if not artifact.exists() or not validation.exists():
            decisions[scenario["id"]] = {"supported": False, "reasons": ["artifact_or_validation_missing"],
                                         "native_validation": json.loads(validation.read_text()) if validation.exists() else None}
            continue
        try:
            native_validation = json.loads(validation.read_text())
            decision = project_user_seed(artifact.read_bytes(), native_validation, scenario)
            decision.update(artifact_path=str(artifact.resolve()), validation_path=str(validation.resolve()),
                            native_validation=native_validation)
        except (ValueError, TypeError, KeyError):
            decision = {"supported": False, "reasons": ["malformed_artifact_or_validation"]}
        decisions[scenario["id"]] = decision
    Path(run_dir).mkdir(parents=True, exist_ok=True)
    _write_json(Path(run_dir) / "inventory.json", decisions)
    return scenarios, decisions


def report(panel, artifacts, run_dir):
    run_dir = Path(run_dir)
    scenarios, decisions = inventory(panel, artifacts, run_dir)
    traces = [trace for path in sorted((run_dir / "cells").glob("*/*/traces.jsonl")) for trace in read_jsonl(path)]
    rows = coverage_rows(scenarios, decisions, traces)
    _write_json(run_dir / "coverage.json", {"scenario_count": len(scenarios), "cell_count": len(rows),
                "total_target_requests": len(traces), "rows": rows})
    with (run_dir / "coverage.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else ["scenario_id"])
        writer.writeheader()
        writer.writerows({k: json.dumps(v) if isinstance(v, list) else v for k, v in row.items()} for row in rows)
    lines = ["# Asago scenarios / actual MiniKlarna coverage", "",
        "This run uses the user's pre-generated Asago scenario files. Originals are copied unchanged and hashed. No Policy Mapper or Scenario Generator runs are needed. Fixture adaptations are recorded separately in the execution manifest.", "",
        f"Requested: {len(scenarios)} scenario IDs × 2 modes = {len(rows)} cells. Actual target requests: {len(traces)}.",
        f"Selected for initial live execution: {sum(s.get('execution_selected', True) for s in scenarios)}/{len(scenarios)} scenarios. Unselected scenarios remain in the coverage denominator and are not security passes.", "",
        "Scenarios become attacker-side agent.yaml configuration artifacts through our adapter. AgentBreaker generates its own attacks; we retain the first initial attack to bound each adversarial cell to two target requests. Native verification and refinement use the same actual OGX/MCP backend. This is scenario-driven AgentBreaker, not native Asago ToolChat replay. The target's system context and real tools are unchanged.", "",
        "| Scenario | Mode | Applicability / selected | Target requests | Runtime errors | Observed classifications |",
        "|---|---|---|---:|---:|---|"]
    by_id = {s['id']: s for s in scenarios}
    for row in rows:
        s = by_id[row['scenario_id']]
        lines.append(f"| {row['scenario_id']} | {row['mode']} | {s.get('applicability', 'not reviewed')} / {s.get('execution_selected', True)} | {row['target_requests']} | {row['runtime_errors']} | {', '.join(row['classifications']) or 'not executed'} |")
    lines += ["", "A native judge alert can reflect willingness or a mistaken factual premise. Security outcomes require actual tool/state evidence. Missing evidence and failed execution remain unknown. The refund metric still has four policies; selected cases do not establish coverage of every policy. Shared local Ollama use prevents a timing comparison with the original run.", "",
              "See inventory.json for every artifact gate and coverage.json / coverage.csv for every requested cell. Per-cell logs, traces, artifacts and native judgments are under cells/."]
    (run_dir / "report.md").write_text("\n".join(lines) + "\n")
    return rows


def run(panel, artifacts, config, run_dir, only=None):
    run_dir = Path(run_dir).resolve()
    scenarios, decisions = inventory(panel, artifacts, run_dir)
    selected = [s for s in scenarios if not only or s["id"] in only]
    frozen = {"panel_sha256": hashlib.sha256(Path(panel).read_bytes()).hexdigest(),
              "config_sha256": hashlib.sha256(Path(config).read_bytes()).hexdigest(),
              "scenario_hashes": {s["id"]: hashlib.sha256((Path(panel).parent / s["scenario_file"]).read_bytes()).hexdigest() for s in scenarios},
              "source_lock_sha256": hashlib.sha256((ROOT / "asago.sources.lock.json").read_bytes()).hexdigest()}
    frozen_path = run_dir / "execution-inputs.json"
    if frozen_path.exists() and json.loads(frozen_path.read_text()) != frozen:
        raise ValueError("Execution inputs changed; use a separate run directory")
    _write_json(frozen_path, frozen)
    state_path = run_dir / "execution-status.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"status": "running", "cells": {}}
    state["status"] = "running"
    _write_json(state_path, state)
    inputs = run_dir / "cell-inputs"
    inputs.mkdir(exist_ok=True)
    for scenario in selected:
        for mode in ("safe", "unsafe"):
            key = f'{scenario["id"]}/{mode}'
            if key in state["cells"]:
                continue  # Never silently rerun a failed or completed state-changing request.
            if scenario.get("execution_selected") is False:
                state["cells"][key] = {"status": "not_selected", "reason": scenario.get("selection_reason", "Not selected for this bounded pilot")}
                _write_json(state_path, state)
                continue
            used = sum(len(read_jsonl(path)) for path in (run_dir / "cells").glob("*/*/traces.jsonl"))
            cell_cap = 2 if scenario["category"] == "adversarial" else 1
            if used + cell_cap > 40:
                raise ValueError("Pilot total target request budget exhausted")
            scenario_file = inputs / f'{scenario["id"]}.json'
            _write_json(scenario_file, dict(scenario, scenario_document=(Path(panel).parent / scenario["scenario_file"]).read_text()))
            output = run_dir / "cells" / scenario["id"] / mode
            output.parent.mkdir(parents=True, exist_ok=True)
            if output.exists():
                raise ValueError("Untracked cell output exists; inspect it before continuing")
            command = [str(ROOT / ".venv-ogx/bin/python"), "-m", "agentbreaker_eval.asago_worker", "--config", str(Path(config).resolve()),
                       "--scenario", str(scenario_file), "--mode", mode, "--output", str(output)]
            environment = worker_environment("0.17.0", output)
            for name in ("OPENAI_API_KEY", "LITELLM_API_KEY", "HF_TOKEN", "GOOGLE_API_KEY", "GEMINI_API_KEY"):
                environment.pop(name, None)
            environment["PYTHONUNBUFFERED"] = "1"
            state["cells"][key] = {"status": "running", "started_at": time.time(), "command": command}
            _write_json(state_path, state)
            print(json.dumps({"event": "cell_started", "cell": key}), flush=True)
            log = output.parent / f"{mode}.log"
            with log.open("wb") as handle:
                process = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    code = process.wait(timeout=2400)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=15)
                    code = 124
            state["cells"][key].update(status="completed" if code == 0 else "failed", exit_code=code, finished_at=time.time(), log=str(log))
            _write_json(state_path, state)
            report(panel, artifacts, run_dir)
            print(json.dumps({"event": "cell_finished", "cell": key, "exit_code": code}), flush=True)
    all_cells = len(scenarios) * 2
    states = [v["status"] for v in state["cells"].values()]
    state["status"] = ("partial" if len(states) < all_cells or any(s != "completed" for s in states) else "completed")
    _write_json(state_path, state)
    report(panel, artifacts, run_dir)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inventory", "run", "report"])
    parser.add_argument("--panel", default="configs/asago/pregenerated/execution-manifest.json")
    parser.add_argument("--artifacts", default="runs/asago-pregenerated-klarna-pilot/native-artifacts")
    parser.add_argument("--config", default="configs/asago/ogx-qwen.json")
    parser.add_argument("--run-dir", default="runs/asago-pregenerated-klarna-pilot")
    parser.add_argument("--only", action="append")
    args = parser.parse_args(argv)
    if args.command == "run":
        result = run(args.panel, args.artifacts, args.config, args.run_dir, args.only)
        print(json.dumps({"status": result["status"]}))
    else:
        (report if args.command == "report" else inventory)(args.panel, args.artifacts, args.run_dir)


if __name__ == "__main__":
    main()
