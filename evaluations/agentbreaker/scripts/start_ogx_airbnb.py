"""Start the isolated Airbnb OGX process on :8322. Leaves Klarna :8321 alone."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentbreaker_eval.runner import load_env  # noqa: E402


def main() -> int:
    load_env()
    if not os.getenv("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is missing from the environment / .env", file=sys.stderr)
        return 1
    import certifi
    storage = ROOT / "runs/ogx-airbnb-runtime/storage"
    config_dir = ROOT / "runs/ogx-airbnb-runtime/config"
    log_path = ROOT / "runs/ogx-airbnb-runtime/server.log"
    storage.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["OGX_CONFIG_DIR"] = str(config_dir)
    env["OGX_EVAL_STORAGE_DIR"] = str(storage)
    env.setdefault("SSL_CERT_FILE", certifi.where())
    env.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    proxy = ROOT / "scripts/gemini_openai_proxy.py"
    proxy_log = ROOT / "runs/ogx-airbnb-runtime/proxy.log"
    proxy_log.parent.mkdir(parents=True, exist_ok=True)
    proxy_handle = proxy_log.open("ab")
    subprocess.Popen(
        [sys.executable, str(proxy)],
        cwd=ROOT,
        env=env,
        stdout=proxy_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    ogx = ROOT / ".venv-ogx/bin/ogx"
    log = log_path.open("ab")
    process = subprocess.Popen(
        [str(ogx), "run", str(ROOT / "configs/ogx-airbnb.yaml"), "--insecure"],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(json_info(process.pid, log_path))
    return 0


def json_info(pid, log_path):
    import json
    return json.dumps({"pid": pid, "port": 8322, "log": str(log_path), "klarna_port_untouched": 8321})


if __name__ == "__main__":
    raise SystemExit(main())
