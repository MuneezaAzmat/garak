"""Model roster loading and strict role/identity validation.

The roster (models, attacker/target pairs and the fixed judge) is read from the
JSON file named by ``AGENTIC_MODELS``, defaulting to ``configs/models.json``.
Configuration names are not evidence that a provider served a particular model.
Resolved deployment metadata is recorded separately after provider preflight.
"""

from copy import deepcopy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDERS = ("openai-compatible", "ollama")
ROLES = ("target", "attacker", "helper", "judge")


def roster_path() -> Path:
    """Return the selected roster file."""
    return Path(os.environ.get("AGENTIC_MODELS", ROOT / "configs" / "models.json"))


def load_roster(path: Path | None = None) -> dict:
    """Load and validate a roster before any manifest can reference it."""
    path = Path(path or roster_path())
    roster = json.loads(path.read_text())
    models, pairs, judge = roster.get("models"), roster.get("pairs"), roster.get("judge")
    if not isinstance(models, dict) or not models:
        raise ValueError(f"{path}: roster must define models")
    for key, spec in models.items():
        if spec.get("provider") not in PROVIDERS:
            raise ValueError(f"{path}: {key} provider must be one of {PROVIDERS}")
        if not spec.get("model") and not spec.get("model_env"):
            raise ValueError(f"{path}: {key} needs model or model_env")
        if spec["provider"] == "openai-compatible" and not (
            spec.get("base_url") or spec.get("base_url_env")
        ):
            raise ValueError(f"{path}: {key} needs base_url or base_url_env")
        if not set(spec.get("roles", ())) <= set(ROLES) or not spec.get("roles"):
            raise ValueError(f"{path}: {key} roles must be drawn from {ROLES}")
        spec.setdefault("name", key)
    if judge not in models or "judge" not in models[judge]["roles"]:
        raise ValueError(f"{path}: judge must name a model with the judge role")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError(f"{path}: roster must define attacker/target pairs")
    if len({pair.get("row_id") for pair in pairs}) != len(pairs):
        raise ValueError(f"{path}: pair row_id values must be unique")
    return {"models": models, "pairs": tuple(pairs), "judge": judge}


_ROSTER = load_roster()
MODEL_SPECS = _ROSTER["models"]
PRIMARY_PAIRS = _ROSTER["pairs"]
JUDGE = _ROSTER["judge"]


def validate_roles(roles: dict) -> None:
    """Reject roles a model is not declared for. Models may be shared across roles."""
    if set(roles) != set(ROLES):
        raise ValueError(
            "Exactly target, attacker, helper, and judge roles are required"
        )
    for role, key in roles.items():
        if key not in MODEL_SPECS or role not in MODEL_SPECS[key]["roles"]:
            raise ValueError(f"Unsupported model for {role}: {key}")
    if roles["judge"] != JUDGE:
        raise ValueError(f"The fixed judge must be {JUDGE}")
    if roles["helper"] != roles["attacker"]:
        raise ValueError("The helper must use its associated attacker")


def requested_models(roles: dict) -> dict:
    """Return independent requested specifications without resolved claims."""
    validate_roles(roles)
    return {role: deepcopy(MODEL_SPECS[key]) for role, key in roles.items()}


def validate_resolved_identity(requested: dict, resolved: dict) -> None:
    """Check preflight metadata without interpreting client aliases as proof.

    A model fixed in the roster (``model`` without ``model_env``) must be the
    model requested. Local Ollama deployments must record digest, quantisation
    and serving backend, because a mutable model tag is insufficient; pin them
    with ``pinned_digest``, ``pinned_quantization`` and ``pinned_backend``.
    """
    required = ("provider", "request_model", "returned_model", "verified_at")
    if any(not resolved.get(field) for field in required):
        raise ValueError("Resolved identity is incomplete")
    if resolved["provider"] != requested["provider"]:
        raise ValueError("Provider substitution is not permitted")
    fixed = None if requested.get("model_env") else requested.get("model")
    if fixed and resolved["request_model"] != fixed:
        raise ValueError("Resolved request model does not match requested model")
    expected = (
        resolved["request_model"]
        if requested["provider"] == "ollama"
        else resolved.get("expected_returned_model", resolved["request_model"])
    )
    if resolved["returned_model"] != expected:
        raise ValueError("Provider returned an unexpected model identity")
    if requested["provider"] == "ollama":
        if any(not resolved.get(key) for key in ("digest", "quantization", "backend")):
            raise ValueError(
                "Local model digest, quantization, and backend are required"
            )
        for field in ("digest", "quantization", "backend"):
            pinned = requested.get(f"pinned_{field}")
            if pinned and resolved[field] != pinned:
                raise ValueError(f"Local model {field} changed from its pinned value")
    elif not resolved.get("identity_evidence"):
        raise ValueError("Hosted identity verification evidence is required")
