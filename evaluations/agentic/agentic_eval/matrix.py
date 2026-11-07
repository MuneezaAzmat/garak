"""Generate deterministic, matched evaluation manifests without model calls."""

from hashlib import sha256
import json
from pathlib import Path

from .models import JUDGE, PRIMARY_PAIRS, requested_models


DOMAINS = ("klarna", "airbnb", "occiai")
ARMS = ("agent-breaker", "toolchain")


def load_experiment_configs() -> dict:
    """Load JSON-formatted YAML and reject changes to the frozen primary design."""
    directory = Path(__file__).resolve().parents[1] / "configs" / "experiments"
    configs = {}
    for arm in ARMS:
        config = json.loads((directory / f"{arm}.yaml").read_text())
        expected_components = [arm]
        expected = {
            "schema_version": 1,
            "arm": arm,
            "components": expected_components,
            "allocation": "equal-fixed-no-borrowing",
            "backend_mode": "unsafe",
            "parser_helper": "attacker",
            "domains": list(DOMAINS),
            "information_condition": "native-instructions-and-schemas",
            "pilot": {"caps": [10]},
            "main": {"caps": [10, 50]},
            "share_component_artifacts": False,
            "counterbalance_component_order": len(expected_components) == 2,
            "offline_judge_comparison": False,
            "self_attack_diagnostics": False,
        }
        if config != expected:
            raise ValueError(
                f"Experiment config changes the frozen primary design: {arm}"
            )
        configs[arm] = config
    return configs


def stable_id(value: dict) -> str:
    """Hash a canonical JSON record for stable identities."""
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def build_matrix(stage: str = "pilot") -> list[dict]:
    """Build the declared pilot or main design with unresolved deployments.

    Each configuration runs once on the native default world, without repeats.
    Deployments must be resolved before execution.
    """
    if stage not in {"pilot", "main"}:
        raise ValueError("stage must be pilot or main")
    configs = load_experiment_configs()
    caps = configs[ARMS[0]][stage]["caps"]
    manifests = []
    for pair in PRIMARY_PAIRS:
        roles = {
            "target": pair["target"],
            "attacker": pair["attacker"],
            "helper": pair["attacker"],
            "judge": JUDGE,
        }
        for domain in DOMAINS:
            for cap in caps:
                paired = {
                    "stage": stage,
                    "row_id": pair["row_id"],
                    "domain": domain,
                    "block_id": f"{stage}-00",
                    "cap": cap,
                }
                for arm in ARMS:
                    components = list(configs[arm]["components"])
                    manifest = {
                        "schema_version": 1,
                        **paired,
                        "pair_id": stable_id(paired),
                        "world": "upstream-default",
                        "agent_manifest_hash": None,
                        "backend_mode": "unsafe",
                        "information_condition": "native-instructions-and-schemas",
                        "arm": arm,
                        "component_caps": {
                            component: cap // len(components)
                            for component in components
                        },
                        "component_order": components,
                        "roles": dict(roles),
                        "requested_models": requested_models(roles),
                        "resolved_models": {},
                        "source_revision": None,
                    }
                    manifest["run_id"] = stable_id(manifest)
                    manifests.append(manifest)
    return manifests
