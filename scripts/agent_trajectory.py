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
    parser.add_argument("--max-steps", type=int, default=25)
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
    )
    runner = AgentTrajectoryRunner(
        goal_png=Path(args.goal),
        output_dir=Path(args.output_dir),
        vlm=vlm,
        max_steps=args.max_steps,
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
    return 0 if result.success or result.terminated_by == "agent" else 1


if __name__ == "__main__":
    raise SystemExit(main())
