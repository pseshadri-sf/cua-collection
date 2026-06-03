# Wave-11 ablation — fixed planner, 100 assets (cost / score / time)

Run: `runs/wave11_ablation_20260603T142545Z` (65 FC + 35 BL; FC hardest-65
dipping into medium, BL = entire library). Fixed Gemini-3.1-pro planner +
qwen3-vl-30b, `python_eval`, `--max-retries 0`, 4 workers.

**100/100 valid builds (96 succeeded + 4 loop_killed), 0 failures, 0 timeouts**
— the newDocument/loop fix eliminated the timeouts seen pre-fix.

## Performance (match_score)
| | mean |
|---|---|
| FreeCAD (n=65) | **60.6** |
| Blender (n=35) | **86.9** |
| Overall (n=100) | **69.8** |
(Note: lower-than-wave10 FC is expected — this set dips into medium-difficulty
FC assets, score range by design.)

## Cost (USD per asset, planner + SLM)
| | per asset | total (100) |
|---|---|---|
| Planner (Gemini 3.1 Pro, low) | $0.0210 | $2.10 |
| Small LLM (qwen3-vl-30b) | $0.0042 | $0.42 |
| **TOTAL** | **$0.0252** | **$2.52** |

FC $0.0269/asset · BL $0.0220/asset. **Planner is ~83% of spend** (one
Gemini call dominates the ~handful of cheap SLM calls — the fix's
"emit-once-then-terminate" keeps SLM calls low).

## Time (wall-clock)
- avg per asset: **78 s** (FC 88 s · BL 60 s)
- total wall-clock: **32.9 min** (100 assets / 4 workers)
- sum of per-asset durations: 2.2 h

## Takeaways
- Whole pipeline is cheap (~2.5¢/asset) and fast (~1.3 min/asset); the frontier
  planner is the cost driver but it's the source of the quality lift.
- 0 timeouts confirms the emit-once fix; SLM cost stays tiny because trajectories
  are short now.
- Next (deferred): best-of-3 with per-asset plan cache to confirm the score lift
  vs wave-9.1 best-of-3.
