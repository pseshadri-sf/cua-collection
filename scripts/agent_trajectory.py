"""CLI entry point for the agentic trajectory collection harness."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cua_smoketest.agent.runner import AgentTrajectoryRunner   # noqa: E402
from cua_smoketest.agent.vlm_client import OpenRouterVLMClient  # noqa: E402


def _load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-trajectory",
        description="Run a VLM-driven trajectory toward GOAL_STATE in FreeCAD.",
    )
    parser.add_argument("--goal", required=True,
                        help="Path to goal state PNG.")
    parser.add_argument("--output-dir", default=str(Path.home() / "cua_agent_runs" / "default"),
                        help="Where to write trajectory.mp4, trajectory.json, frames/, logs/.")
    parser.add_argument("--model", default="google/gemma-4-31b-it",
                        help="OpenRouter model id.")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--env-file", default="/home/ubuntu/.env",
                        help="Path to a .env file containing the API key.")
    parser.add_argument("--api-key-var", default="OPENAI_API_KEY",
                        help="Env var name holding the OpenRouter API key.")
    parser.add_argument("--provider", action="append", default=None,
                        help="OpenRouter provider preference (repeatable). "
                             "Example: --provider DeepInfra --provider Chutes.")
    parser.add_argument("--ignore-provider", action="append", default=None,
                        help="OpenRouter provider to skip (repeatable). "
                             "Example: --ignore-provider Novita.")
    parser.add_argument("--strict-provider", action="store_true",
                        help="Disable fallback to other providers if the "
                             "preferred ones fail.")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"),
                        default="high",
                        help="OpenRouter reasoning.effort. low = much faster, "
                             "fewer tokens; high = more deliberative.")
    parser.add_argument("--image-max-dim", type=int, default=1920,
                        help="Longest screenshot edge (px) sent to the VLM. "
                             "1024 cuts vision tokens ~3x vs the default 1920.")
    parser.add_argument("--escalate-at-step", type=int, default=0,
                        help="If >0, switch reasoning effort to "
                             "--escalate-to-effort once the agent has taken "
                             "this many steps without self-terminating. "
                             "0 = no escalation.")
    parser.add_argument("--escalate-to-effort", choices=("low", "medium", "high"),
                        default="high",
                        help="Reasoning effort to switch to on escalation.")
    parser.add_argument("--structured-actions", action="store_true",
                        help="v1 experiment: prepend a preamble to the Qwen "
                             "reminder that prefers typed build_*/cut/fuse/compound "
                             "actions over raw python_eval. Default OFF.")
    parser.add_argument("--tool-calling", action="store_true",
                        help="Wave-1: enable OpenRouter tools=[...] for the "
                             "typed build_*/cut/fuse/compound actions. Replaces "
                             "JSON-action-in-content with structured tool_calls.")
    parser.add_argument("--few-shot", action="store_true",
                        help="Wave-1: prepend few-shot exemplars distilled from "
                             "high-scoring prior trajectories.")
    parser.add_argument("--tool-calling-required", action="store_true",
                        help="Wave-2: tool_choice='required' + python_eval "
                             "excluded from tools — force typed action emission.")
    parser.add_argument("--few-shot-delayed", action="store_true",
                        help="Wave-2: 2-example few-shot injected only from "
                             "step 2+ to avoid first-action anchoring.")
    # Wave-3 grounded-prompt interventions
    parser.add_argument("--dim-estimate", action="store_true",
                        help="Wave-3: require BBOX_ESTIMATE: prefix in rationale.")
    parser.add_argument("--force-bool-on-voids", action="store_true",
                        help="Wave-3: prompt rule to use cut pattern when goal has voids.")
    parser.add_argument("--count-parts", action="store_true",
                        help="Wave-3: require PART_COUNT: N in rationale + multi-part decomposition.")
    parser.add_argument("--no-box-bias", action="store_true",
                        help="Wave-3: shape-taxonomy rule to counter build_box default.")
    parser.add_argument("--grounded", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="Wave-4 (default ON): inject pre-computed bbox + "
                             "object_count + primitive_class metadata from "
                             "<goal>.meta.json into the per-turn user message. "
                             "Sidecar JSONs are produced by "
                             "scripts/extract_goal_metadata.py. Pass --no-grounded "
                             "to disable (returns to pre-Wave-4 behavior).")
    parser.add_argument("--decompose", action="store_true",
                        help="Wave-4.1: when the sidecar has a `parts` list "
                             "(object_count > 1, built via "
                             "build_goal_metadata_sidecars.py --decompose), "
                             "append a per-part bbox+origin table to "
                             "GOAL_METADATA so the agent can emit one build_* "
                             "per part instead of one big primitive.")
    parser.add_argument("--planner-model", default=None,
                        help="frontier-onepass: OpenRouter model id for the "
                             "one-pass frontier planner (e.g. "
                             "google/gemini-3-pro-preview). When set, the "
                             "frontier model plans the whole build at step 0 and "
                             "the plan guides the executor (Variant A).")
    parser.add_argument("--planner-reasoning", choices=("low","medium","high"),
                        default="low",
                        help="Reasoning effort for the frontier planner call.")
    parser.add_argument("--plan-format", choices=("python_eval", "build_star"),
                        default="python_eval",
                        help="How the BUILD_PLAN is surfaced downstream: a ready "
                             "one-line python_eval reconstruction (default) or "
                             "per-step build_* primitive ops.")
    parser.add_argument("--compositional", action="store_true",
                        help="compositional_dynamics: replay the plan per-component "
                             "(one python_eval each) for a fine-grained build video.")
    args = parser.parse_args(argv)

    # Load API key: env file overrides process env only if not already set.
    if args.env_file:
        for k, v in _load_env_file(Path(args.env_file)).items():
            os.environ.setdefault(k, v)
    api_key = os.environ.get(args.api_key_var, "")
    if not api_key:
        print(f"ERROR: {args.api_key_var} not found in env or {args.env_file}",
              file=sys.stderr)
        return 2

    vlm = OpenRouterVLMClient(
        api_key=api_key, model=args.model,
        provider_order=args.provider,
        provider_ignore=args.ignore_provider,
        allow_fallbacks=not args.strict_provider,
        reasoning_effort=args.reasoning_effort,
        image_max_dim=args.image_max_dim,
        app="freecad",
        structured_actions=args.structured_actions,
        tool_calling=args.tool_calling,
        tool_calling_required=args.tool_calling_required,
        few_shot=args.few_shot,
        few_shot_delayed=args.few_shot_delayed,
        dim_estimate=args.dim_estimate,
        force_bool_on_voids=args.force_bool_on_voids,
        count_parts=args.count_parts,
        no_box_bias=args.no_box_bias,
        grounded=args.grounded,
        decompose=args.decompose,
    )
    runner = AgentTrajectoryRunner(
        goal_png=Path(args.goal),
        output_dir=Path(args.output_dir),
        vlm=vlm,
        max_steps=args.max_steps,
        escalate_at_step=args.escalate_at_step,
        escalate_to_effort=args.escalate_to_effort,
        planner_model=args.planner_model,
        plan_format=args.plan_format,
        compositional=args.compositional,
        planner_reasoning=args.planner_reasoning,
    )
    result = runner.run()

    print("\n========== AGENT TRAJECTORY REPORT ==========")
    print(f"Goal:          {result.goal_path}")
    print(f"Video:         {result.video_path}")
    print(f"JSON:          {result.json_path}")
    print(f"Steps:         {len(result.steps)} (incl step 0 init)")
    print(f"Terminated by: {result.terminated_by}")
    print(f"Success:       {result.success}")
    if result.error:
        print(f"Error:         {result.error}")
    # Wave-9: loop-kill fires AFTER a valid build is on disk (the geometry is
    # real and often correct), so it is not a crash — exit 0. The orchestrator
    # records the distinct `loop_killed` status from trajectory.json.
    clean = result.success or result.terminated_by in ("agent", "agent_loop_detected")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
