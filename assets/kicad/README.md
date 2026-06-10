# KiCad seed assets

`_blank.kicad_pcb` — the empty 2-layer board pcbnew opens as the agent's working
document. The agent builds INTO a copy of this (it never opens the goal board),
mirroring how the FreeCAD/Blender tracks reconstruct from an empty start.

The board format (`version 20221018`) is validated against the installed KiCad
during the M0 infra spike (see SPEC-kicad-pipeline.md §2.1). If pcbnew refuses to
open it on the target KiCad, regenerate with `kicad-cli`/the GUI and replace this
file; `KiCadAgentTrajectoryRunner._prepare_working_board` falls back to an
inline template if this file is absent.
