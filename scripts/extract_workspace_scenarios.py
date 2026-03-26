#!/usr/bin/env python3
"""Extract workspace scenarios from AgentDojo for use as Garak probes.

Runs AgentDojo's ground-truth pipeline (no LLM) against the injected workspace
environment to capture the exact tool responses each user task sees. Outputs
Python data structures ready for SUITE_SCENARIOS["workspace"] in
garak/probes/agentdojo.py.

Usage:
    python3 scripts/extract_workspace_scenarios.py
"""

import json
import sys
import traceback

from agentdojo.task_suite.load_suites import get_suite
from agentdojo.agent_pipeline.ground_truth_pipeline import GroundTruthPipeline
from agentdojo.agent_pipeline.tool_execution import tool_result_to_str
from agentdojo.functions_runtime import FunctionsRuntime
from agentdojo.attacks.important_instructions_attacks import ImportantInstructionsAttack
from agentdojo.attacks.base_attacks import FixedJailbreakAttack
from agentdojo.types import get_text_content_as_str


CANARY = "___PAYLOAD_PLACEHOLDER___"


def extract_workspace_scenarios():
    suite = get_suite("v1", "workspace")

    # Get injection vector defaults to know all placeholder names
    injection_defaults = suite.get_injection_vector_defaults()
    print(f"Injection vectors ({len(injection_defaults)}):")
    for k, v in injection_defaults.items():
        print(f"  {k}: {v[:60]}...")
    print()

    # Build canary injections — we use a unique canary per vector so we can
    # identify which vectors each user task sees
    canary_injections = {k: f"<CANARY_{k}>" for k in injection_defaults}

    # Load environment with canaries
    env_with_canaries = suite.load_and_inject_default_environment(canary_injections)

    # Extract injection task info
    print("=" * 80)
    print("INJECTION TASKS")
    print("=" * 80)
    for task_id, task in sorted(suite.injection_tasks.items()):
        print(f"\n{task_id}:")
        print(f"  GOAL: {task.GOAL}")
        print(f"  DIFFICULTY: {task.DIFFICULTY}")
        gt = task.ground_truth(env_with_canaries.model_copy(deep=True))
        print(f"  GROUND_TRUTH ({len(gt)} calls):")
        for call in gt:
            print(f"    {call.function}({call.args})")

    # Extract user task scenarios
    print("\n" + "=" * 80)
    print("USER TASKS")
    print("=" * 80)

    scenarios = []
    errors = []

    for task_id, task in sorted(suite.user_tasks.items()):
        print(f"\n--- {task_id} ---")
        print(f"  PROMPT: {task.PROMPT[:100]}...")

        try:
            # Run ground truth pipeline against canary environment
            gt_pipeline = GroundTruthPipeline(task)
            runtime = FunctionsRuntime(suite.tools)
            env_copy = env_with_canaries.model_copy(deep=True)

            _, _, _, messages, _ = gt_pipeline.query(
                task.PROMPT, runtime, env_copy
            )

            # Find tool result messages that contain canaries
            tool_responses_with_canaries = []
            seen_vectors = set()

            for msg in messages:
                if msg["role"] == "tool":
                    content = get_text_content_as_str(msg["content"])
                    for vec_name in canary_injections:
                        if canary_injections[vec_name] in content:
                            seen_vectors.add(vec_name)
                    if any(canary_injections[v] in content for v in canary_injections):
                        tool_responses_with_canaries.append(content)

            if not tool_responses_with_canaries:
                print(f"  WARNING: No tool responses with canaries found")
                # Still collect all tool responses
                all_tool_responses = []
                for msg in messages:
                    if msg["role"] == "tool":
                        content = get_text_content_as_str(msg["content"])
                        all_tool_responses.append(content)
                print(f"  All tool responses ({len(all_tool_responses)}):")
                for tr in all_tool_responses:
                    print(f"    {tr[:100]}...")
                errors.append(task_id)
                continue

            # Replace canaries with {payload} placeholder
            combined_response = "\n\n".join(tool_responses_with_canaries)
            for vec_name in canary_injections:
                combined_response = combined_response.replace(
                    canary_injections[vec_name], "{payload}"
                )

            print(f"  Injection vectors seen: {sorted(seen_vectors)}")
            print(f"  Tool responses with injection: {len(tool_responses_with_canaries)}")
            print(f"  Combined response (first 200): {combined_response[:200]}...")

            scenarios.append({
                "user_task_id": task_id,
                "user_prompt": task.PROMPT,
                "tool_context": combined_response,
                "injection_vectors": sorted(seen_vectors),
            })

        except Exception as e:
            print(f"  ERROR: {e}")
            traceback.print_exc()
            errors.append(task_id)

    # Print summary
    print("\n" + "=" * 80)
    print(f"SUMMARY: {len(scenarios)} scenarios extracted, {len(errors)} errors")
    print("=" * 80)

    if errors:
        print(f"  Errors: {errors}")

    # Output as Python code for SUITE_SCENARIOS
    print("\n" + "=" * 80)
    print("PYTHON OUTPUT (paste into SUITE_SCENARIOS['workspace'])")
    print("=" * 80)

    print('"tool_response_templates": [')
    for sc in scenarios:
        # Escape quotes in the tool_context
        tool_context_escaped = sc["tool_context"].replace("\\", "\\\\").replace('"', '\\"')
        print(f'    {{')
        print(f'        "user_prompt": {json.dumps(sc["user_prompt"])},')
        print(f'        "user_task_id": {json.dumps(sc["user_task_id"])},')
        print(f'        "tool_context": (')
        # Split into manageable lines
        lines = sc["tool_context"].split("\n")
        for i, line in enumerate(lines):
            line_escaped = json.dumps(line)
            if i < len(lines) - 1:
                print(f'            {line_escaped} "\\n"')
            else:
                print(f'            {line_escaped}')
        print(f'        ),')
        print(f'    }},')
    print('],')

    # Also dump as JSON for easier consumption
    output_path = "scripts/workspace_scenarios.json"
    with open(output_path, "w") as f:
        json.dump({
            "scenarios": scenarios,
            "injection_tasks": {
                tid: {
                    "goal": task.GOAL,
                    "difficulty": str(task.DIFFICULTY),
                }
                for tid, task in suite.injection_tasks.items()
            },
            "injection_vectors": list(injection_defaults.keys()),
        }, f, indent=2)
    print(f"\nJSON output saved to {output_path}")


if __name__ == "__main__":
    extract_workspace_scenarios()
