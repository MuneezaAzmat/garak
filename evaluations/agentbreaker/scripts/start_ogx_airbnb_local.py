"""Start Airbnb OGX on :8322 using local Ollama qwen2.5:14b. Leaves Klarna :8321 alone."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentbreaker_eval.runner import load_env  # noqa: E402


def main() -> int:
    load_env()
    import certifi
    storage = ROOT / "runs/ogx-airbnb-local-runtime/storage"
    config_dir = ROOT / "runs/ogx-airbnb-local-runtime/config"
    log_path = ROOT / "runs/ogx-airbnb-local-runtime/server.log"
    storage.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["OGX_CONFIG_DIR"] = str(config_dir)
    env["OGX_EVAL_STORAGE_DIR"] = str(storage)
    env.setdefault("SSL_CERT_FILE", certifi.where())
    env.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    ogx = ROOT / ".venv-ogx/bin/ogx"
    log = log_path.open("ab")
    process = subprocess.Popen(
        [str(ogx), "run", str(ROOT / "configs/ogx-airbnb-local.yaml"), "--insecure"],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(json.dumps({"pid": process.pid, "port": 8322, "log": str(log_path), "klarna_port_untouched": 8321}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
