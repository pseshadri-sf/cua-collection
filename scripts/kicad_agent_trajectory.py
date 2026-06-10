"""CLI entry point for the KiCad (pcbnew) agentic trajectory harness."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if _SRC.exists() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from cua_smoketest.agent.kicad_runner import KiCadAgentTrajectoryRunner  # noqa: E402
from cua_smoketest.agent.vlm_client import OpenRouterVLMClient            # noqa: E402


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
        prog="kicad-agent-trajectory",
        description="Run a VLM-driven trajectory toward GOAL_STATE in KiCad pcbnew.",
    )
    parser.add_argument("--goal", required=True)
    parser.add_argument("--output-dir",
                        default=str(Path.home() / "cua_agent_runs" / "kicad_default"))
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
    parser.add_argument("--escalate-at-step", type=int, default=0)
    parser.add_argument("--escalate-to-effort", choices=("low", "medium", "high"),
                        default="high")
    parser.add_argument("--grounded", action=argparse.BooleanOptionalAction,
                        default=True,
                        help="Default ON: inject pre-computed GOAL_METADATA from "
                             "<goal>.meta.json. Pass --no-grounded to disable.")
    parser.add_argument("--decompose", action="store_true",
                        help="Append the per-footprint table to GOAL_METADATA "
                             "when the sidecar has a kicad.footprints list.")
    parser.add_argument("--seed-board", default=None,
                        help="Blank .kicad_pcb the agent builds into (default: "
                             "assets/kicad/_blank.kicad_pcb, else a built-in template).")
    parser.add_argument("--planner-model", default=None,
                        help="OpenRouter model id for the one-pass frontier "
                             "planner (pcbnew layout plan for KiCad).")
    parser.add_argument("--planner-reasoning", choices=("low", "medium", "high"),
                        default="low")
    parser.add_argument("--plan-format", choices=("python_eval", "build_star"),
                        default="python_eval",
                        help="How the BUILD_PLAN is surfaced downstream.")
    parser.add_argument("--compositional", action="store_true",
                        help="Replay the plan per-component (one pcbnew_eval each).")
    parser.add_argument("--best-of-both", action="store_true",
                        help="(Blender only; accepted+ignored on KiCad for arg-compat.)")
    parser.add_argument("--postprocess", action="store_true",
                        help="After the run, emit video_clean.mp4 (canvas-only, "
                             "code-entry cut, recalibrated timestamps).")
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
        app="kicad",
        grounded=args.grounded,
        decompose=args.decompose,
    )
    runner = KiCadAgentTrajectoryRunner(
        goal_png=Path(args.goal),
        output_dir=Path(args.output_dir),
        vlm=vlm,
        max_steps=args.max_steps,
        escalate_at_step=args.escalate_at_step,
        escalate_to_effort=args.escalate_to_effort,
        seed_board=Path(args.seed_board) if args.seed_board else None,
        planner_model=args.planner_model,
        plan_format=args.plan_format,
        planner_reasoning=args.planner_reasoning,
        compositional=args.compositional,
        postprocess=args.postprocess,
    )
    result = runner.run()

    print("\n========== KICAD AGENT TRAJECTORY REPORT ==========")
    print(f"Goal:          {result.goal_path}")
    print(f"Video:         {result.video_path}")
    print(f"JSON:          {result.json_path}")
    print(f"Steps:         {len(result.steps)} (incl step 0 init)")
    print(f"Terminated by: {result.terminated_by}")
    print(f"Success:       {result.success}")
    if result.error:
        print(f"Error:         {result.error}")
    clean = result.success or result.terminated_by in ("agent", "agent_loop_detected")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
