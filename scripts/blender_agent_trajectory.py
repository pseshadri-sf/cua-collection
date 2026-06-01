"""CLI entry point for the Blender agentic trajectory harness."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cua_smoketest.agent.blender_runner import BlenderAgentTrajectoryRunner  # noqa: E402
from cua_smoketest.agent.vlm_client import OpenRouterVLMClient                # noqa: E402


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
        prog="blender-agent-trajectory",
        description="Run a VLM-driven trajectory toward GOAL_STATE in Blender.",
    )
    parser.add_argument("--goal", required=True)
    parser.add_argument("--output-dir",
                        default=str(Path.home() / "cua_agent_runs" / "blender_default"))
    parser.add_argument("--model", default="google/gemma-4-31b-it")
    parser.add_argument("--max-steps", type=int, default=15)
    parser.add_argument("--env-file", default="/home/ubuntu/.env")
    parser.add_argument("--api-key-var", default="OPENAI_API_KEY")
    parser.add_argument("--provider", action="append", default=None)
    parser.add_argument("--ignore-provider", action="append", default=None)
    parser.add_argument("--strict-provider", action="store_true")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"),
                        default="high")
    parser.add_argument("--image-max-dim", type=int, default=1920)
    parser.add_argument("--escalate-at-step", type=int, default=0,
                        help="If >0, escalate reasoning effort after N steps.")
    parser.add_argument("--escalate-to-effort", choices=("low", "medium", "high"),
                        default="high")
    parser.add_argument("--structured-actions", action="store_true",
                        help="v1: prefer typed build_*/cut/fuse over python_eval.")
    parser.add_argument("--tool-calling", action="store_true",
                        help="Wave-1: enable OpenRouter tools=[...] for typed actions.")
    parser.add_argument("--few-shot", action="store_true",
                        help="Wave-1: prepend few-shot exemplars.")
    parser.add_argument("--tool-calling-required", action="store_true",
                        help="Wave-2: force typed-tool emission.")
    parser.add_argument("--few-shot-delayed", action="store_true",
                        help="Wave-2: 2-example few-shot from step 2+.")
    parser.add_argument("--dim-estimate", action="store_true")
    parser.add_argument("--force-bool-on-voids", action="store_true")
    parser.add_argument("--count-parts", action="store_true")
    parser.add_argument("--no-box-bias", action="store_true")
    parser.add_argument("--grounded", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="Wave-4 (default ON): inject pre-computed GOAL_METADATA "
                             "from <goal>.meta.json sidecar. Pass --no-grounded to "
                             "disable.")
    args = parser.parse_args(argv)

    if args.env_file:
        for k, v in _load_env_file(Path(args.env_file)).items():
            os.environ.setdefault(k, v)
    api_key = os.environ.get(args.api_key_var, "")
    if not api_key:
        print(f"ERROR: {args.api_key_var} not found", file=sys.stderr)
        return 2

    vlm = OpenRouterVLMClient(
        api_key=api_key, model=args.model,
        provider_order=args.provider,
        provider_ignore=args.ignore_provider,
        allow_fallbacks=not args.strict_provider,
        reasoning_effort=args.reasoning_effort,
        image_max_dim=args.image_max_dim,
        app="blender",
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
    )
    runner = BlenderAgentTrajectoryRunner(
        goal_png=Path(args.goal),
        output_dir=Path(args.output_dir),
        vlm=vlm,
        max_steps=args.max_steps,
        escalate_at_step=args.escalate_at_step,
        escalate_to_effort=args.escalate_to_effort,
    )
    result = runner.run()

    print("\n========== BLENDER AGENT TRAJECTORY REPORT ==========")
    print(f"Goal:          {result.goal_path}")
    print(f"Video:         {result.video_path}")
    print(f"JSON:          {result.json_path}")
    print(f"Steps:         {len(result.steps)} (incl step 0 init)")
    print(f"Terminated by: {result.terminated_by}")
    print(f"Success:       {result.success}")
    if result.error:
        print(f"Error:         {result.error}")
    return 0 if result.success or result.terminated_by == "agent" else 1


if __name__ == "__main__":
    raise SystemExit(main())
