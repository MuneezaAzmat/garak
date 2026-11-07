"""Reproduce the local evaluation environment from tracked source and uv locks.

Running without --check-only may fetch the pinned mini-agent checkout and install
locked Python dependencies. It never downloads models, runs evaluations or pushes.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


class BootstrapError(RuntimeError):
    """A source, environment or patch check prevented reproducible setup."""


def run(
    command: list[str],
    *,
    cwd: Path,
    env: dict | None = None,
    allowed: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess:
    """Execute argument arrays without shell interpolation."""
    completed = subprocess.run(
        command, cwd=cwd, env=env, text=True, capture_output=True, check=False
    )
    if completed.returncode not in allowed:
        raise BootstrapError(
            f"Command failed ({completed.returncode}): {' '.join(command)}\n{completed.stderr.strip()}"
        )
    return completed


def load_lock(root: Path) -> dict:
    """Load required immutable source pins from the tracked lock."""
    lock = json.loads((root / "sources.lock.json").read_text())
    for name in ("garak", "mini_agents"):
        revision = lock[name]["revision"]
        if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
            raise BootstrapError(f"{name} revision must be a full lowercase commit SHA")
    if lock.get("schema_version") != 1:
        raise BootstrapError("Unsupported source lock schema")
    return lock


def local_environment(root: Path) -> dict:
    """Place uv cache, managed Python and the environment inside this checkout."""
    env = dict(os.environ)
    env.update(
        {
            "UV_CACHE_DIR": str(root / ".cache" / "uv"),
            "UV_PYTHON_INSTALL_DIR": str(root / ".python"),
            "UV_PROJECT_ENVIRONMENT": str(root / ".venv"),
            "UV_LINK_MODE": "copy",
        }
    )
    return env


def verify_garak(root: Path, revision: str) -> str:
    """Allow evaluation commits descended from the pinned probe implementation."""
    repo = root.parents[1]
    result = run(
        ["git", "merge-base", "--is-ancestor", revision, "HEAD"],
        cwd=repo,
        allowed=(0, 1),
    )
    if result.returncode:
        raise BootstrapError(
            "Current Garak HEAD does not contain the pinned ToolChain revision"
        )
    return run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()


def verify_dependency(path: Path, revision: str) -> None:
    """Refuse modified or mismatched dependency checkouts; never reset them."""
    if not path.is_dir() or not (path / ".git").exists():
        raise BootstrapError(f"Pinned mini-agent Git checkout is missing: {path}")
    dirty = run(
        ["git", "status", "--porcelain", "--untracked-files=all"], cwd=path
    ).stdout
    if dirty.strip():
        raise BootstrapError(
            "mini-agents checkout has local changes; preserve/reconcile them before bootstrap"
        )
    head = run(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()
    if head != revision:
        raise BootstrapError(
            f"mini-agents HEAD {head} differs from lock {revision}; bootstrap will not switch an existing checkout"
        )


def ensure_dependency(root: Path, lock: dict, *, check_only: bool) -> Path:
    """Fetch an absent dependency at the exact revision, or validate an existing one."""
    path = root / ".deps" / "mini-agents"
    source = lock["mini_agents"]
    if not path.exists():
        if check_only:
            raise BootstrapError(
                "mini-agents is absent; normal bootstrap is required to fetch it"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--no-checkout", source["url"], str(path)], cwd=root)
        run(["git", "fetch", "origin", source["revision"]], cwd=path)
        run(["git", "checkout", "--detach", source["revision"]], cwd=path)
    verify_dependency(path, source["revision"])
    return path


def verify_hash(path: Path, expected: str) -> str:
    """Reject changed source/archive bytes rather than accepting a moving URL."""
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise BootstrapError(
            f"SHA256 mismatch for {path.name}: expected {expected}, got {actual}"
        )
    return actual


def verify_patch_markers(script: Path, streaming: Path) -> str:
    """Ensure every locked replacement landed despite upstream's permissive exit code."""
    replacements = None
    for node in ast.parse(script.read_text()).body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "REPLACEMENTS"
        ):
            replacements = ast.literal_eval(node.value)
            break
    if replacements is None:
        raise BootstrapError(
            "Pinned patch no longer exposes the expected replacement table"
        )
    content = streaming.read_text()
    missing = [
        index
        for index, (_, _, marker) in enumerate(replacements, 1)
        if marker not in content
    ]
    if missing:
        raise BootstrapError(
            f"OGX patch incomplete; missing replacement markers {missing}"
        )
    return hashlib.sha256(streaming.read_bytes()).hexdigest()


def find_uv(explicit: str | None) -> str:
    """Accept an explicit uv executable or resolve it through PATH."""
    candidate = explicit or shutil.which("uv")
    if candidate is None:
        raise BootstrapError("uv is not on PATH; supply --uv /absolute/path/to/uv")
    path = Path(candidate).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise BootstrapError(f"uv executable is unavailable: {path}")
    return str(path)


def install_environment(root: Path, lock: dict, patch: Path, uv: str) -> dict:
    """Synchronise frozen dependencies and apply/verify the locked OGX patch."""
    env = local_environment(root)
    run(
        [uv, "sync", "--frozen", "--python", "3.12", "--project", str(root)],
        cwd=root,
        env=env,
    )
    python = (
        root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    metadata_code = """import importlib.metadata as m, json, sys
from pathlib import Path
paths = list(Path(sys.prefix, "lib").glob("python*/site-packages/ogx/providers/inline/responses/builtin/responses/streaming.py"))
if not paths:
    paths = list(Path(sys.prefix, "Lib").glob("site-packages/ogx/providers/inline/responses/builtin/responses/streaming.py"))
print(json.dumps({"python": list(sys.version_info[:3]), "ogx": m.version("ogx"), "ogx_api": m.version("ogx-api"), "streaming": str(paths[0]) if len(paths) == 1 else None}))"""
    info = json.loads(run([str(python), "-c", metadata_code], cwd=root, env=env).stdout)
    if (
        info["python"][:2] != [3, 12]
        or info["ogx"] != lock["ogx"]
        or info["ogx_api"] != lock["ogx_api"]
    ):
        raise BootstrapError("Installed Python/OGX versions differ from source lock")
    if not info["streaming"]:
        raise BootstrapError("Cannot locate exactly one installed OGX streaming module")
    # The pinned script is idempotent. Check every marker independently because
    # its exit status alone does not establish complete application.
    run([str(python), str(patch)], cwd=root, env=env)
    info["patched_streaming_sha256"] = verify_patch_markers(
        patch, Path(info.pop("streaming"))
    )
    info["uv_version"] = run([uv, "--version"], cwd=root, env=env).stdout.strip()
    return info


def bootstrap(
    root: Path,
    *,
    check_only: bool = False,
    uv: str | None = None,
) -> dict:
    """Verify sources and optionally reproduce the locked local environment."""
    root = root.resolve()
    lock = load_lock(root)
    head = verify_garak(root, lock["garak"]["revision"])
    ensure_dependency(root, lock, check_only=check_only)
    patch = (root / lock["ogx_patch"]["path"]).resolve()
    if not patch.is_relative_to(root):
        raise BootstrapError("Patch path escapes evaluation checkout")
    patch_hash = verify_hash(patch, lock["ogx_patch"]["sha256"])
    receipt = {
        "schema_version": 1,
        "garak_head": head,
        "mini_agents_revision": lock["mini_agents"]["revision"],
        "patch_sha256": patch_hash,
        "mode": "sources-only" if check_only else "environment",
        "model_downloads_performed": False,
    }
    if not check_only:
        receipt["environment"] = install_environment(root, lock, patch, find_uv(uv))
        output = root / ".cache" / "bootstrap-receipt.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main() -> int:
    """Run setup without coupling it to provider calls or evaluation execution."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Verify existing source pins and patch bytes; do not fetch, sync or patch",
    )
    parser.add_argument(
        "--uv", help="Explicit uv executable path when uv is not on PATH"
    )
    args = parser.parse_args()
    try:
        receipt = bootstrap(
            ROOT,
            check_only=args.check_only,
            uv=args.uv,
        )
    except (BootstrapError, OSError, ValueError, KeyError) as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
