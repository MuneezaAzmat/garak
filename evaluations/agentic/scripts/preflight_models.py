"""Verify explicit provider identities without exposing credential values."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic_eval.models import MODEL_SPECS
from agentic_eval.providers import load_credentials, preflight_model, ProviderError

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--env-file", type=Path)
parser.add_argument(
    "--models", nargs="+", choices=MODEL_SPECS, default=list(MODEL_SPECS)
)
parser.add_argument("--catalogue-only", action="store_true")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
load_credentials(args.env_file)
result = {"models": {}, "errors": {}}
for model in args.models:
    try:
        result["models"][model] = preflight_model(
            model, inference=not args.catalogue_only
        )
        print(
            f"{model}: verified {'catalogue' if args.catalogue_only else 'inference'}",
            flush=True,
        )
    except (ProviderError, ValueError) as exc:
        result["errors"][model] = str(exc)
        print(f"{model}: {exc}", flush=True)
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(result, indent=2) + "\n")
raise SystemExit(bool(result["errors"]))
