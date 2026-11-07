"""Prepare or execute a reproducible pilot or main study."""

import argparse
import json
from pathlib import Path
import sys
from threading import Event, Thread

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic_eval.providers import load_credentials
from agentic_eval.runner import prepare_batch, run_batch, run_pilot
from watch_progress import refresh

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("command", choices=["prepare", "run", "pilot"])
parser.add_argument(
    "--stage",
    choices=["pilot", "main"],
    default="pilot",
    help="Study to prepare; execution reads the saved matrix",
)
parser.add_argument(
    "--arm",
    choices=["agent-breaker", "toolchain", "both"],
    default="agent-breaker",
    help="Arms to prepare; ToolChain is deferred pending its main-branch merge",
)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--preflight", type=Path)
selection = parser.add_mutually_exclusive_group()
selection.add_argument(
    "--limit", type=int, help="Run the first N prepared rows; omit for the whole batch"
)
selection.add_argument("--run-id", help="Run only this exact prepared matrix row")
parser.add_argument("--env-file", type=Path)
args = parser.parse_args()
load_credentials(args.env_file)
if args.command == "prepare":
    preflight = json.loads(args.preflight.read_text()) if args.preflight else None
    matrix = prepare_batch(
        args.output,
        preflight,
        stage=args.stage,
        arm=None if args.arm == "both" else args.arm,
    )
    print(
        f"Prepared {len(matrix)} {args.stage} manifests ({args.arm}); no attacks executed."
    )
else:
    stop = Event()

    def monitor():
        while not stop.wait(3):
            refresh(args.output)

    thread = Thread(target=monitor, daemon=True)
    thread.start()
    try:
        execute = run_pilot if args.command == "pilot" else run_batch
        execute(args.output, limit=args.limit, run_id=args.run_id)
    finally:
        stop.set()
        thread.join()
        if (args.output / "matrix.json").exists():
            refresh(args.output)
