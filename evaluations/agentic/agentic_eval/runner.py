"""Serial development-pilot execution with durable artifacts and evidence labels."""

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import threading
import time
import traceback
from uuid import uuid4

from .analysis import by_stratum, summarise
from .budget import Budget
from .display import eval_id, write_views
from .evidence import evaluate, prepare_agent
from .matrix import build_matrix, stable_id
from .models import MODEL_SPECS, requested_models, validate_resolved_identity
from .ogx import GatewayError, reply_text
from .providers import ProviderError, chat_generator, endpoint

ROOT = Path(__file__).resolve().parents[1]
LIMITS = {
    "max_tool_calls": 16,
    "max_model_calls": 16,
    "request_timeout": 900,
    "target_output_tokens": 8192,
    "attacker_output_tokens": 16384,
    "judge_output_tokens": 8192,
    "role_call_limit": 200,
    "role_retry_delays_seconds": [2, 5],
}


def configure_local_paths():
    """Keep Garak's runtime caches inside the ignored evaluation directory."""
    for kind in ("CONFIG", "CACHE", "DATA"):
        path = ROOT / ".cache" / "garak" / kind.lower()
        path.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault(f"XDG_{kind}_HOME", str(path))


def write_json(path: Path, value):
    """Atomically replace an artifact with canonical JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def protocol_hash() -> str:
    """Fingerprint executable evaluation code, policies, prompts and dependency locks.

    Git-ignored files are local-only and excluded, so the fingerprint depends on
    the shared source. The roster in use is recorded in each manifest instead.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--"]
        + ["agentic_eval", "configs", "policies"]
        + ["sources.lock.json", "uv.lock", "pyproject.toml"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout.decode()
    paths = [
        ROOT / name
        for name in listed.split("\0")
        if name and "__pycache__" not in Path(name).parts and (ROOT / name).is_file()
    ]
    receipt = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(paths)
    }
    return stable_id(receipt)


def source_provenance(source: dict) -> str:
    """Verify pinned native Garak files and return the actual checkout commit."""
    repository = ROOT.parents[1]
    revision = source["garak"]["revision"]
    commands = (
        ["git", "rev-parse", "HEAD"],
        ["git", "diff", "--quiet", revision, "--", "garak"],
        ["git", "ls-files", "--others", "--exclude-standard", "--", "garak"],
    )
    results = [
        subprocess.run(
            command,
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        for command in commands
    ]
    if any(result.returncode for result in results) or results[2].stdout.strip():
        raise ValueError(
            "Native Garak source differs from the pinned revision or cannot be verified"
        )
    return results[0].stdout.strip()


def prepare_batch(
    directory: Path, preflight: dict | None = None, *, stage="pilot", arm=None
) -> list[dict]:
    """Prepare native agent manifests; unresolved deployments remain non-executable."""
    directory = Path(directory).resolve()
    source = json.loads((ROOT / "sources.lock.json").read_text())
    checkout_revision = source_provenance(source)
    matrix = build_matrix(stage)
    if arm is not None:
        matrix = [row for row in matrix if row["arm"] == arm]
        if not matrix:
            raise ValueError("Unknown probe arm")
    agents = {
        domain: prepare_agent(domain, directory / "agents" / domain)
        for domain in ("klarna", "airbnb", "occiai")
    }
    resolved = (preflight or {}).get("models", {})
    fingerprint = protocol_hash()
    for manifest in matrix:
        agent = agents[manifest["domain"]]
        manifest["agent_manifest_hash"] = agent["manifest_hash"]
        manifest["source_revision"] = source
        manifest["checkout_revision"] = checkout_revision
        manifest["protocol_hash"] = fingerprint
        manifest["limits"] = dict(LIMITS)
        manifest["resolved_models"] = {
            role: resolved[key]
            for role, key in manifest["roles"].items()
            if key in resolved
        }
        manifest["run_id"] = stable_id(
            {k: v for k, v in manifest.items() if k != "run_id"}
        )
    path = directory / "matrix.json"
    if path.exists() and json.loads(path.read_text()) != matrix:
        raise ValueError("Existing matrix differs; use a new batch directory")
    write_json(path, matrix)
    if not (directory / "progress.json").exists():
        write_json(
            directory / "progress.json",
            {
                "status": "Not started",
                "processed_runs": 0,
                "selected_runs": len(matrix),
                "scheduled_runs": len(matrix),
            },
        )
    write_views(directory, matrix)
    return matrix


def validate_executable(manifest):
    """Reject incomplete deployment identity or a changed protocol before execution."""
    if manifest.get("run_id") != stable_id(
        {key: value for key, value in manifest.items() if key != "run_id"}
    ):
        raise ValueError("Manifest identity does not match its contents")
    if manifest["requested_models"] != requested_models(manifest["roles"]):
        raise ValueError("Model roster changed since preparation")
    if manifest.get("backend_mode") != "unsafe":
        raise ValueError("Only the agreed unsafe backend is executable")
    locked_source = json.loads((ROOT / "sources.lock.json").read_text())
    if manifest["source_revision"] != locked_source:
        raise ValueError("Source lock changed since preparation")
    if manifest["checkout_revision"] != source_provenance(locked_source):
        raise ValueError("Checkout commit changed since preparation")
    if manifest["protocol_hash"] != protocol_hash():
        raise ValueError("Protocol changed since matrix preparation")
    for role, key in manifest["roles"].items():
        resolved = manifest["resolved_models"].get(role, {})
        validate_resolved_identity(MODEL_SPECS[key], resolved)
        if not resolved.get("inference_verified"):
            raise ValueError(f"{role} inference preflight has not passed")
    if manifest["resolved_models"]["helper"] != manifest["resolved_models"]["attacker"]:
        raise ValueError("Helper deployment must equal its associated attacker")


class Recorder:
    """Persist evidence as it arrives; data never enters online inputs implicitly."""

    def __init__(self, path, run_id):
        self.path, self.run_id = Path(path), run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.events = []
        self.lock = threading.Lock()

    def activity(self, role, state):
        """Expose an in-flight role without counting it as a completed model call."""
        write_json(
            self.path.parent / "activity.json",
            {"role": role, "state": state, "updated_at": time.time()},
        )

    def __call__(self, event):
        value = {
            "schema_version": 1,
            "event_id": uuid4().hex,
            "timestamp": time.time(),
            **event,
            "run_id": self.run_id,
            "event_type": event.get("event_type", event.get("event", "unspecified")),
        }
        with self.lock:
            self.events.append(value)
            with self.path.open("a") as stream:
                stream.write(json.dumps(value) + "\n")
            if value["event_type"] in ("target_request", "target_response"):
                self.activity(
                    "target",
                    "waiting" if value["event_type"] == "target_request" else "idle",
                )


def execute_run(
    manifest,
    directory: Path,
    *,
    runtime_factory=None,
    probe_executor=None,
    generator_factory=chat_generator,
):
    """Execute one manifest; never automatically replay an interrupted trajectory."""
    configure_local_paths()
    if runtime_factory is None:
        from .runtime import UnsafeAgentRuntime

        runtime_factory = UnsafeAgentRuntime
    if probe_executor is None:
        from .probes import run_probe

        probe_executor = run_probe
    validate_executable(manifest)
    directory = Path(directory)
    result_path = directory / "result.json"
    if result_path.exists():
        previous = json.loads(result_path.read_text())
        if previous["manifest"] != manifest:
            raise ValueError("Stored result belongs to another configuration")
        return previous
    directory.mkdir(parents=True, exist_ok=True)
    started = directory / "started.json"
    if started.exists():
        raise RuntimeError(
            "Interrupted run requires evidence reconciliation; refusing automatic replay"
        )
    try:
        with started.open("x") as stream:
            json.dump(
                {
                    "run_id": manifest["run_id"],
                    "execution_id": uuid4().hex,
                    "started_at": time.time(),
                },
                stream,
            )
    except FileExistsError:
        raise RuntimeError(
            "Run already claimed; refusing concurrent execution"
        ) from None
    write_json(directory / "manifest.json", manifest)
    agent = prepare_agent(manifest["domain"], directory / "agent")
    if agent["manifest_hash"] != manifest["agent_manifest_hash"]:
        raise ValueError("Agent context/tools differ from prepared manifest")
    recorder = Recorder(directory / "events.jsonl", manifest["run_id"])
    budget = Budget(
        directory / "requests.sqlite", manifest["cap"], manifest["component_caps"]
    )
    status, components, first_success = "completed", [], None
    limits = manifest["limits"]
    role_calls = 0

    def role_recorder(event):
        nonlocal role_calls
        role_calls += 1
        recorder(event)

    def check_role_budget():
        if role_calls >= limits["role_call_limit"]:
            recorder(
                {
                    "event_type": "role_call_limit",
                    "status": "provider_failure",
                    "visibility": "evaluator",
                    "payload": {"limit": limits["role_call_limit"]},
                }
            )
            raise ProviderError("Per-run attacker/helper/judge call limit reached")

    class LimitedGenerator:
        def __init__(self, generator):
            self.generator = generator

        def __getattr__(self, name):
            return getattr(self.generator, name)

        def generate(self, *args, **kwargs):
            check_role_budget()
            return self.generator.generate(*args, **kwargs)

    try:
        attacker = LimitedGenerator(
            generator_factory(
                manifest["roles"]["attacker"],
                manifest["resolved_models"]["attacker"],
                role_recorder,
                role="attacker",
                max_tokens=limits["attacker_output_tokens"],
                timeout=limits["request_timeout"],
                retry_delays=tuple(limits["role_retry_delays_seconds"]),
                before_call=check_role_budget,
                activity=lambda state: recorder.activity("attacker", state),
            )
        )
        judge = LimitedGenerator(
            generator_factory(
                manifest["roles"]["judge"],
                manifest["resolved_models"]["judge"],
                role_recorder,
                role="judge",
                max_tokens=limits["judge_output_tokens"],
                timeout=limits["request_timeout"],
                retry_delays=tuple(limits["role_retry_delays_seconds"]),
                before_call=check_role_budget,
                activity=lambda state: recorder.activity("judge", state),
            )
        )
        model, base, key_env = endpoint(manifest["roles"]["target"])
        resolved_target = manifest["resolved_models"]["target"]
        if (
            model != resolved_target["request_model"]
            or base != resolved_target["base_url"]
        ):
            raise ProviderError("Target endpoint changed since preflight")
        for component in manifest["component_order"]:
            # ToolChain trajectories may be interleaved by native breadth-first scheduling.
            # Keep an isolated runtime for each chain. AgentBreaker resets each attempt.
            with ExitStack() as stack:
                runtimes = {}

                def dispatch(prompt, notes):
                    nonlocal first_success
                    chain = notes.get("chain_id") if component == "toolchain" else None
                    if component == "toolchain" and not chain:
                        raise ValueError(
                            "ToolChain dispatch is missing its native chain_id"
                        )
                    trajectory = str(chain or notes.get("attempt_id") or uuid4().hex)
                    runtime_key = str(chain) if chain else "independent-attempt"
                    if runtime_key not in runtimes:
                        state = (
                            directory
                            / component
                            / hashlib.sha256(runtime_key.encode()).hexdigest()[:16]
                        )
                        runtimes[runtime_key] = stack.enter_context(
                            runtime_factory(
                                manifest["domain"],
                                model,
                                base,
                                state,
                                recorder,
                                run_id=manifest["run_id"],
                                api_key_env=key_env,
                                instructions=agent["context"],
                                request_timeout=limits["request_timeout"],
                                max_model_calls=limits["max_model_calls"],
                                max_tool_calls=limits["max_tool_calls"],
                            )
                        )
                        expected = {
                            item["name"]: item["inputSchema"]
                            for item in agent["manifest"]["tools"]
                        }
                        actual = {
                            item["name"]: item["inputSchema"]
                            for item in runtimes[runtime_key].tools()
                        }
                        if actual != expected:
                            raise ValueError(
                                "Runtime tool schemas differ from the pinned manifest"
                            )
                    elif not chain:
                        runtimes[runtime_key].reset()
                    runtime = runtimes[runtime_key]
                    before = runtime.snapshot()
                    request_id = budget.reserve(
                        component, "chain_step" if chain else "attack_or_refinement"
                    )
                    budget.mark_dispatched(request_id)
                    offset = len(recorder.events)
                    try:
                        response = runtime.request(
                            prompt,
                            request_id=request_id,
                            component=component,
                            trajectory_id=trajectory,
                            max_output_tokens=limits["target_output_tokens"],
                        )
                    except (GatewayError, OSError, ValueError, RuntimeError, TypeError):
                        budget.finish(request_id, "indeterminate")
                        raise
                    budget.finish(request_id, "completed")
                    judgment = evaluate(
                        manifest["domain"],
                        before,
                        runtime.snapshot(),
                        recorder.events[offset:],
                    )
                    recorder(
                        {
                            "event_type": "request_outcome",
                            "request_id": request_id,
                            "component": component,
                            "visibility": "evaluator",
                            "payload": judgment,
                        }
                    )
                    if (
                        judgment["label"] == "confirmed_violation"
                        and first_success is None
                    ):
                        first_success = budget.snapshot()["dispatched"]
                    return reply_text(response)

                native = probe_executor(
                    component,
                    Path(agent["manifest_path"]),
                    attacker,
                    judge,
                    dispatch,
                    manifest["component_caps"][component],
                    recorder,
                )
                attempts = native.pop("attempts", [])
                write_json(
                    directory / f"{component}-attempts.json",
                    [attempt.as_dict() for attempt in attempts],
                )
                components.append({"component": component, **native})
                native_status = native.get("status")
                if native.get("unresolved_judgments") or any(
                    value is None
                    for entry in native.get("native_scores", [])
                    for value in entry.get("scores", [])
                ):
                    native_status = "judge_unresolved"
                if native_status not in ("completed", "budget_exhausted"):
                    status = (
                        native_status
                        if native_status in ("judge_unresolved", "no_candidates")
                        else "probe_failure"
                    )
                    if status != "no_candidates":
                        break
                # Native discovery/refinement catches some provider exceptions.
                # Recorded failures must not be silently relabelled no_candidates.
                if any(
                    event.get("status") == "provider_failure"
                    for event in recorder.events
                ):
                    status = "provider_failure"
                    break
    except (
        ProviderError,
        GatewayError,
        OSError,
        ValueError,
        RuntimeError,
        TypeError,
    ) as exc:
        status = (
            "provider_failure"
            if isinstance(exc, ProviderError)
            else "execution_failure"
        )
        recorder(
            {
                "event_type": "run_error",
                "status": status,
                "visibility": "evaluator",
                "payload": {"type": type(exc).__name__, "message": str(exc)},
            }
        )
    if status in ("completed", "no_candidates") and components:
        status = (
            "no_candidates"
            if all(item.get("status") == "no_candidates" for item in components)
            else "completed"
        )
    outcome = evaluate(
        manifest["domain"], None, None, recorder.events, execution_status=status
    )
    usage = {}
    for event in recorder.events:
        details = (
            event.get("response", {}).get("usage")
            or event.get("payload", {}).get("usage")
            or {}
        )
        role = event.get("role", "unknown")
        if details:
            usage[role] = usage.get(role, 0) + details.get("total_tokens", 0)
    result = {
        "manifest": manifest,
        "outcome": outcome,
        "components": components,
        "budget": budget.snapshot(),
        "first_success_request": first_success,
        "counters": {
            "target_requests": budget.snapshot()["dispatched"],
            "role_model_calls": role_calls,
            "role_retry_attempts": sum(
                event.get("event_type") == "role_model_call"
                and event.get("attempt_number", 1) > 1
                for event in recorder.events
            ),
            "token_limited_responses": sum(
                event.get("event_type") == "target_response"
                and event.get("status") == "incomplete"
                for event in recorder.events
            ),
        },
        "usage_by_role": usage,
    }
    write_json(result_path, result)
    return result


def record_failed_run(manifest, directory, exc):
    """Preserve available evidence when a row fails outside normal finalisation."""
    directory.mkdir(parents=True, exist_ok=True)
    failure = {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(),
    }
    write_json(directory / "failure.json", failure)
    events = []
    path = directory / "events.jsonl"
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                # Keep the original damaged/partial log for inspection.
                continue
    status = (
        "provider_failure" if isinstance(exc, ProviderError) else "execution_failure"
    )
    event = {
        "event_type": "run_error",
        "timestamp": time.time(),
        "run_id": manifest["run_id"],
        "status": status,
        "visibility": "evaluator",
        "payload": {"type": type(exc).__name__, "message": str(exc)},
    }
    # Do not merge a new event into a partial line left by a crashed writer.
    if (
        not path.exists()
        or not path.stat().st_size
        or path.read_bytes().endswith(b"\n")
    ):
        with path.open("a") as stream:
            stream.write(json.dumps(event) + "\n")
    events.append(event)
    budget = None
    if (directory / "requests.sqlite").exists():
        try:
            budget = Budget(
                directory / "requests.sqlite",
                manifest["cap"],
                manifest["component_caps"],
            ).snapshot()
        except (sqlite3.Error, OSError, ValueError) as ledger_error:
            failure["ledger_error"] = str(ledger_error)
    requests = [e for e in events if e.get("event_type") == "target_request"]
    # Reuse completed evidence assessments; the exception may be in the scorer.
    assessed = [
        e["payload"] for e in events if e.get("event_type") == "request_outcome"
    ]
    violations = [v for outcome in assessed for v in outcome.get("violations", [])]
    usage = {}
    for event in events:
        details = (
            (event.get("response") or {}).get("usage")
            or event.get("payload", {}).get("usage")
            or {}
        )
        if details:
            role = event.get("role", "unknown")
            usage[role] = usage.get(role, 0) + details.get("total_tokens", 0)
    result = {
        "manifest": manifest,
        "outcome": {
            "execution_status": status,
            "label": "confirmed_violation" if violations else "unresolved",
            "violations": violations,
            "assessed_tool_calls": sum(
                o.get("assessed_tool_calls", 0) for o in assessed
            ),
            "blocked_tool_calls": sum(o.get("blocked_tool_calls", 0) for o in assessed),
            "unresolved_reasons": [
                f"Execution status: {status}",
                "Only saved request assessments retained after row exception",
            ],
        },
        "failure": failure,
        "components": [],
        "budget": budget,
        "first_success_request": None,
        "counters": {
            "target_requests": budget["dispatched"]
            if budget is not None
            else len(requests),
            "target_request_count_source": "ledger"
            if budget is not None
            else "trace_lower_bound",
        },
        "usage_by_role": usage,
    }
    write_json(directory / "result.json", result)
    return result


def run_batch(
    directory: Path,
    limit: int | None = None,
    run_id: str | None = None,
    *,
    expected_stage=None,
):
    """Execute a complete prepared study or an explicitly selected subset."""
    if limit is not None and run_id is not None:
        raise ValueError("Choose either limit or run_id")
    directory = Path(directory).resolve()
    matrix = json.loads((directory / "matrix.json").read_text())
    stages = {row["stage"] for row in matrix}
    if len(stages) != 1 or (expected_stage and stages != {expected_stage}):
        raise ValueError("Batch has an unexpected or mixed study stage")
    stage = next(iter(stages))
    arms = {row["arm"] for row in matrix}
    expected = [row for row in build_matrix(stage) if row["arm"] in arms]
    design_fields = ("stage", "row_id", "domain", "block_id", "cap", "arm")
    design = lambda rows: {tuple(row[key] for key in design_fields) for row in rows}
    expected_count = len(expected)
    if (
        len(matrix) != expected_count
        or len({row["run_id"] for row in matrix}) != expected_count
        or design(matrix) != design(expected)
    ):
        raise ValueError(
            f"Prepared {stage} batch must contain its {expected_count} unique design rows"
        )
    if limit is not None and not 1 <= limit <= len(matrix):
        raise ValueError(f"limit must be between 1 and {len(matrix)}")
    selected = (
        [row for row in matrix if row["run_id"] == run_id] if run_id else matrix[:limit]
    )
    if not selected:
        raise ValueError("Requested run_id is not in this matrix")
    for manifest in selected:
        validate_executable(manifest)
        saved = directory / "runs" / manifest["run_id"] / "result.json"
        if saved.exists() and json.loads(saved.read_text())["manifest"] != manifest:
            raise ValueError("Stored result belongs to another configuration")
    results = []

    def progress(manifest, index, status):
        processed = len(results)
        roles = {
            role: MODEL_SPECS[manifest["roles"][role]]["name"]
            for role in ("target", "attacker", "judge")
        }
        write_json(
            directory / "batch-status.json",
            {
                "updated_at": time.time(),
                "status": status,
                "run_id": manifest["run_id"],
                "selected_runs": len(selected),
                "selected_run_ids": [row["run_id"] for row in selected],
                "processed_runs": processed,
            },
        )
        write_json(
            directory / "progress.json",
            {
                "updated_at": time.time(),
                "status": status,
                "processed_runs": processed,
                "selected_runs": len(selected),
                "scheduled_runs": len(matrix),
                "current_run": index,
                "evaluation_id": eval_id(manifest, index),
                "run_id": manifest["run_id"],
                "row_id": manifest["row_id"],
                "domain": manifest["domain"],
                "arm": manifest["arm"],
                "models": roles,
            },
        )
        write_views(directory, matrix)
        print(
            f"[{eval_id(manifest, index)}] {status} | {processed}/{len(selected)} processed | {manifest['domain']} · {manifest['arm']}",
            flush=True,
        )

    for manifest in selected:
        index = matrix.index(manifest) + 1
        run_directory = directory / "runs" / manifest["run_id"]
        status = (
            "Checking saved result"
            if (run_directory / "result.json").exists()
            else "Running"
        )
        progress(manifest, index, status)
        try:
            result = execute_run(manifest, run_directory)
        except KeyboardInterrupt:
            progress(manifest, index, "Paused")
            raise
        except Exception as exc:
            # The batch boundary isolates arbitrary row failures by user request.
            # Interrupts/SystemExit still propagate; failure-persistence errors do too.
            result = record_failed_run(manifest, run_directory, exc)
        results.append(result)
        write_json(
            directory / "summary.json",
            {
                "overall": summarise(results),
                "strata": by_stratum(results),
                "scheduled_runs": len(matrix),
                "recorded_runs": len(results),
                "complete": len(results) == len(matrix),
            },
        )
        if result["outcome"]["execution_status"] not in ("completed", "no_candidates"):
            progress(
                manifest,
                index,
                "Failed; continuing: " + result["outcome"]["execution_status"],
            )
        else:
            progress(
                manifest, index, "Finished: " + result["outcome"]["execution_status"]
            )
    failed = sum(
        r["outcome"]["execution_status"] not in ("completed", "no_candidates")
        for r in results
    )
    progress(
        selected[-1],
        index,
        "Batch finished with failures" if failed else "Batch finished",
    )
    return results


def run_pilot(directory: Path, limit: int | None = None, run_id: str | None = None):
    """Run a prepared pilot while rejecting accidental main-study execution."""
    return run_batch(directory, limit=limit, run_id=run_id, expected_stage="pilot")
