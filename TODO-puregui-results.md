# pure-gui — GUI-only modelling (Gemini 3 Pro medium, no code, no planner)

Run wave13_puregui (top 25 FC + 25 BL from wave-11). 50/50 completed.
Cost $20.85 ($0.417/asset), ~5.1h wall (4 workers). FC mean 21.6 steps
(13 self-term / 12 max), BL mean 35.1 steps (24/25 max_steps).

## The scores are NOT measurable with the current evaluator
Reported: FC 0.0, BL 45.2 — but BOTH are ARTIFACTS, not the agent's work.
The evaluator reconstructs the agent model by REPLAYING the Python the agent
typed (python_eval / type-text). GUI-only runs emit NO code, so:
- FC: replay of 0 chunks -> empty FreeCAD doc (object_count=0) -> score 0.
- BL: replay of 0 chunks -> Blender's DEFAULT startup cube (object_count=1,
  name 'Cube') -> ~45 is a default-cube-vs-goal bbox floor, NOT the build.
Proven: every pure-gui job has agent_chunks=0. The agent's actual GUI-built
geometry lives only in the live session, which is never saved or replayed.

## What the GUI agent actually did (from action logs + videos)
- Mechanically works: code is hard-blocked (0 successful code actions), and
  Gemini drives the GUI sensibly.
  - FC: switch_workbench(Part) + menu_navigate Part>Primitives (125 menu navs,
    23 workbench switches) to add primitives. BUT only **1 `type` action across
    all 25 FC jobs** — it adds default primitives and barely sets dimensions in
    the property editor (the hard part). 
  - BL: Shift+A add menu + G/S/R keyboard transforms (183 key, 125 hotkey,
    17 type) — far more modelling activity, but 24/25 hit max_steps (no convergence).

## Verdict + required fix
Pure-GUI is mechanically feasible but (a) the agent struggles to set precise
dimensions via GUI, and (b) **it cannot be scored by the current code-replay
evaluator.** To measure it, the runner must SAVE the live document at terminate
(FreeCAD: File>Save As .FCStd; Blender: save .blend) and the eval must score the
SAVED FILE directly instead of replaying chunks. Until then, FC 0 / BL 45 are
not comparable to the code/planner results.
