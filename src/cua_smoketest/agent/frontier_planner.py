"""One-pass frontier planner (frontier-onepass branch, Variant A).

Calls a frontier model (default ``google/gemini-3-pro-preview``) ONCE per asset
with the goal image + GOAL_METADATA and asks for a complete, structured
BUILD_PLAN. The small VLM executor (qwen3-vl-30b) then follows that plan as
authoritative guidance — it still decides each action against the live viewport,
but inherits the frontier model's decomposition + spatial placement (the planning
work the 30B fails at: wave-9d showed stacking + bbox-approximation, never exec
errors).

Plan structure is refined from the earthtojake/text-to-cad skill workflow:
parameters-first, datum-explicit, ordered steps, with concrete validation
targets the runner reconciles against the wave-9.1 S2 AGENT_STATE readback.

The planner emits BOTH a ready-to-run ``full_code`` (one-line python_eval — the
default downstream format) AND per-step ``op/dims/origin`` (used when the
downstream format is switched to ``build_*``). One plan, two renderings.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

_CHAMFER_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "plan_chamfer_score.py"


def _score_candidate(full_code: str, goal_asset: str, blender_bin: str,
                     timeout: float = 200.0) -> float | None:
    """Headless Chamfer score (0-100) of a candidate build vs the goal asset.
    Returns None if scoring could not run (caller treats as lowest priority)."""
    if not _CHAMFER_SCRIPT.exists():
        return None
    try:
        with tempfile.TemporaryDirectory() as td:
            cf = os.path.join(td, "chunks.json")
            out = os.path.join(td, "out.json")
            json.dump([full_code], open(cf, "w"))
            env = {**os.environ, "CH_GOAL": goal_asset, "CH_CHUNKS": cf, "CH_OUT": out}
            subprocess.run([blender_bin, "-b", "-noaudio", "-P", str(_CHAMFER_SCRIPT)],
                           env=env, capture_output=True, timeout=timeout)
            if os.path.exists(out):
                return json.load(open(out)).get("score")
    except Exception:
        return None
    return None

_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
# The Gemini 3 Pro-tier model currently served on OpenRouter is the 3.1 Pro
# preview (plain "gemini-3-pro-preview" has no serving endpoint; the other
# gemini-3 ids are flash / image-gen variants).
DEFAULT_PLANNER_MODEL = "google/gemini-3.1-pro-preview"


# --- planner prompt (text-to-cad conventions baked in) ----------------------

_PLANNER_SYSTEM = """\
You are an expert CAD reconstruction PLANNER for the FreeCAD Part API. You are
given a GOAL image (one or more rendered views of a target solid/assembly) plus
pre-computed GOAL_METADATA. Produce a complete build PLAN that a less-capable
executor agent will follow to recreate the asset in FreeCAD.

CONVENTIONS (follow exactly):
- Units are millimetres. TRUST GOAL_METADATA bbox/part numbers over your own
  estimates from the image; note any disagreement in `brief`.
- Parameters-first: put every dimension in `parameters`, then reference them.
- One distinct visible component = one part. Build every part the goal shows;
  do not collapse an assembly into a single box.
- POSITION EACH PART EXPLICITLY. Every primitive starts at the origin; you MUST
  translate each part to its own location with App.Vector(x,y,z) using the
  per-part origins from GOAL_METADATA. Parts left at the origin stack into one
  blob — the #1 failure mode this plan exists to prevent.
- Keep solids closed and positive-volume. Use booleans (.fuse/.cut/.common) on
  closed operands. Combine parts with Part.makeCompound([...]) (preserves
  separate solids) unless a fused single solid is clearly intended.
- For curved/organic single solids a box is wrong: use makeCylinder, makeSphere,
  makeTorus, makeCone, or Part.makeRevolution on a profile.
- Name the top-level object with GOAL_NAME when provided (earns name credit).

OUTPUT: a single JSON object, no prose outside it, matching this schema:
{
  "brief": "<2-4 sentence natural-language CAD brief: what it is, overall size,
            part breakdown, origin/orientation, key assumptions>",
  "shape_class": "single_solid" | "assembly" | "revolve" | "boolean",
  "parameters": { "<name>": <number>, ... },
  "conventions": { "units": "mm", "origin": "<e.g. base-center>", "up": "+Z" },
  "steps": [
    { "i": 1, "name": "<part_name>", "op": "build_box"|"build_cylinder"|
        "build_sphere"|"build_torus"|"build_cone"|"boolean"|"compound",
      "dims": [<numbers>], "origin": [x,y,z], "why": "<short>" },
    ...
  ],
  "full_code": "<ONE-LINE, semicolon-joined, console-ready python_eval that
      reconstructs the ENTIRE asset. It MUST be IDEMPOTENT — re-running it must
      NOT create extra documents. Start by reusing+clearing ONE document:
      import Part,FreeCAD as App; doc=App.ActiveDocument or App.newDocument();
      [doc.removeObject(o.Name) for o in list(doc.Objects)]; <build+translate
      every part>; <compound/boolean>; o=doc.addObject('Part::Feature','<name>');
      o.Shape=<final>;doc.recompute() — NEVER call App.newDocument()
      unconditionally; NO newlines, NO def/for-loops that span lines>",
  "validation_targets": { "object_count": <int>, "bbox_mm": [x,y,z] },
  "fallback": "<simplest acceptable single-primitive approximation + est. score>"
}

The `full_code` MUST be valid one-line Python that runs in the FreeCAD Python
console as-is. Prefer a list+loop joined on one line only via
`for p in [a,b,c]:` style is NOT one-line-safe — instead translate each part on
its own statement. Double-check every part has a translate.
"""

# Blender variant: same schema, but full_code is one-line bpy.
_PLANNER_SYSTEM_BL = """\
You are an expert Blender (bpy) 3D-reconstruction PLANNER. You are given a GOAL
image (multi-view render of a target mesh) plus GOAL_METADATA. Produce a build
PLAN a less-capable executor will follow to recreate the asset in Blender.

SCALE & PLACEMENT (most important — do not get this wrong):
- TRUST GOAL_METADATA bbox and proportions ABSOLUTELY. Build at the GOAL's real
  size: the final bbox MUST match GOAL_METADATA bbox on every axis. The example
  numbers below are PROPORTIONS only — multiply them to the goal's actual bbox;
  never emit a tiny unit-scale object for a large goal.
- Clear the scene first: import bpy; bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete()
- Give each distinct object its own location=(x,y,z); never stack at the origin.
- Name the top object GOAL_NAME when provided. Parameters-first in `parameters`.

shape_class (classify by the SAME rules as before — keep it stable):
- "assembly"    = multiple distinct parts (chairs, vehicles, appliances, furniture).
- "array"       = a repeated element (slats, fences, keys).
- "single_solid"= one connected object (a bottle, a tool, a box).
- "organic"     = a smooth/curved natural form (animals, fruit, upholstery).

BASE APPROACH (proven, default): build each part from primitives placed and
scaled to the goal — primitive_cube_add(size=,location=), uv_sphere_add(radius=,
location=), cylinder_add(radius=,depth=,location=), cone_add, torus_add,
monkey_add; scale the active object for non-uniform boxes; a one-line list
comprehension for arrays. This already works well for assemblies & hard-surface;
DO NOT over-complicate a shape that a few clean primitives capture.

ENHANCE FIDELITY (add these ONLY where the FORM clearly needs it — they raise
realism but add failure risk, so use sparingly and always APPLY modifiers so the
geometry is real, e.g. m=o.modifiers.new('s','SUBSURF'); m.levels=2; bpy.context.view_layer.objects.active=o; bpy.ops.object.modifier_apply(modifier=m.name)):
- SMOOTH / rounded organic body -> SUBSURF (levels 1-2) + bpy.ops.object.shade_smooth() on the base primitive. Best lever for "organic".
- BILATERALLY SYMMETRIC organic form -> model one half + MIRROR modifier (use_axis), apply.
- REVOLVED vessel (bottle/vase/cup/lamp) -> a profile + SCREW modifier (use a few
  profile verts via bmesh OR a small cylinder + SUBSURF). Prefer this over a plain
  cylinder for clearly curved vessels.
- THIN-WALLED shell (cup/bowl/case) -> SOLIDIFY (thickness).
- ROUNDED EDGES on hard-surface -> BEVEL (width, segments).
- LONG repetition -> ARRAY modifier instead of N primitives.
- TUBES / handles / legs / necks -> a CURVE: primitive_bezier_curve_add then set
  data.bevel_depth, then convert to mesh.
- HOLES / cuts -> BOOLEAN (DIFFERENCE) with a cutter, apply, delete cutter.
- AVOID fragile edit-mode face-deletion / heavy bmesh surgery — prefer modifiers;
  they fail far less often.

ROUTING: assembly/array -> base approach (clean primitives), adding SUBSURF/BEVEL
only on parts that are visibly rounded. single_solid -> base primitive + the ONE
or two modifiers matching its form. organic -> base primitive + SUBSURF + shade_smooth
(+ MIRROR if symmetric); curves for limbs/necks. When unsure, prefer the simpler
build — a correct clean primitive beats a broken fancy one.

OUTPUT: a single JSON object, no prose outside it:
{
  "brief": "<2-4 sentences: what it is, overall size (state the goal bbox), part breakdown, symmetry, which enhancement(s) used and why, assumptions>",
  "shape_class": "single_solid" | "assembly" | "array" | "organic",
  "parameters": { "<name>": <number>, ... },
  "conventions": { "units": "blender", "origin": "<world-center>", "up": "+Z" },
  "steps": [
    { "i": 1, "name": "<part>", "op": "primitive_*|curve|array|subsurf|mirror|solidify|bevel|screw|boolean",
      "dims": [<numbers>], "origin": [x,y,z], "why": "<short>" },
    ...
  ],
  "full_code": "<ONE-LINE, semicolon-joined, console-ready bpy that clears the scene then builds the ENTIRE asset at the GOAL bbox scale, APPLYING any modifiers; list comprehensions OK; NO newlines, NO def/multi-line for>",
  "validation_targets": { "object_count": <int>, "bbox": [x,y,z] },
  "fallback": "<simplest acceptable primitive approximation + est. score>"
}

ENHANCEMENT EXAMPLES (PROPORTIONS — rescale to the goal bbox!):
- organic body: ...primitive_uv_sphere_add(radius=R); o=bpy.context.active_object; o.scale=(0.7,0.7,1.0); m=o.modifiers.new('s','SUBSURF'); m.levels=2; bpy.ops.object.modifier_apply(modifier=m.name); bpy.ops.object.shade_smooth()
- revolved vase: ...primitive_cylinder_add(radius=R,depth=H); o=bpy.context.active_object; sub=o.modifiers.new('s','SUBSURF'); sub.levels=2; bpy.ops.object.modifier_apply(modifier=sub.name); sol=o.modifiers.new('sh','SOLIDIFY'); sol.thickness=0.05*R; bpy.ops.object.modifier_apply(modifier=sol.name)
- slatted array: ...primitive_cube_add(size=1); o=bpy.context.active_object; o.scale=(LX,LY,LZ); a=o.modifiers.new('arr','ARRAY'); a.count=6; a.relative_offset_displace=(0,1.5,0); bpy.ops.object.modifier_apply(modifier=a.name)

The `full_code` MUST run as-is, match the GOAL bbox scale, and APPLY every
modifier. Prefer the simplest construction that matches the goal.
"""

# KiCad variant: PCB layout reconstruction for the pcbnew SWIG API. Mirrors the
# FreeCAD planner's conventions (mm units, reuse-and-clear the live document for
# idempotency, explicit per-part placement) since KiCad's scripting model is the
# closest analogue. full_code is a multi-line pcbnew program (exec'd from a file
# by both the GUI executor and the headless reconstructor) with a P() helper —
# real boards have dozens of footprints, so a per-footprint helper is required.
_PLANNER_SYSTEM_KICAD = """\
You are an expert PCB-layout reconstruction PLANNER for the KiCad pcbnew SWIG
API. You are given a GOAL image (rendered views of a target PCB: top copper,
silkscreen, 3D) plus pre-computed GOAL_METADATA. Produce a complete build PLAN
that a less-capable executor agent will follow to recreate the board in pcbnew.

CONVENTIONS (follow exactly):
- Units are millimetres; ALWAYS wrap with pcbnew.FromMM(...) and positions with
  pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y)). Raw ints are nanometres.
- TRUST GOAL_METADATA (board outline, footprint count, net count, layer count,
  per-footprint ref/position/orientation) over your own estimates from the
  image; note any disagreement in `brief`.
- Parameters-first: put key dimensions/counts in `parameters`, then reference them.
- BUILD ORDER: board outline (PCB_SHAPE on Edge.Cuts) -> place footprints ->
  add nets -> route tracks (PCB_TRACK) -> pour zones (ZONE).
- POSITION EVERY FOOTPRINT EXPLICITLY with SetPosition(...). Footprints left at
  (0,0) stack into one pile — the #1 failure mode this plan exists to prevent.
  Use the per-footprint positions from GOAL_METADATA.
- Reference layers by name via board.GetLayerID('F.Cu'|'B.Cu'|'Edge.Cuts'); set a
  track/zone net via board.FindNet(name).
- Name top-level nets using the goal's net names when provided.

OUTPUT: a single JSON object, no prose outside it, matching this schema:
{
  "brief": "<2-4 sentences: what the board is, overall size (state the outline
            bbox in mm), footprint breakdown, net/layer counts, key assumptions>",
  "shape_class": "pcb_layout",
  "parameters": { "<name>": <number>, ... },
  "conventions": { "units": "mm", "origin": "<e.g. board top-left>", "layers": <int> },
  "steps": [
    { "i": 1, "name": "<outline|R1|track_GND|zone_GND|...>",
      "op": "set_board_outline"|"place_footprint"|"route_track"|"add_via"|
            "add_zone"|"add_net",
      "at": [x_mm, y_mm], "rot": <deg>, "layer": "<F.Cu|B.Cu|Edge.Cuts>",
      "why": "<short>" },
    ...
  ],
  "full_code": "<a complete, MULTI-LINE Python program (real newlines REQUIRED —
      it is exec'd from a file, NOT typed) that reconstructs the ENTIRE board.
      IDEMPOTENT: start by reusing+clearing the open board. Define a P() helper
      and call it ONCE PER FOOTPRINT using the FPID + position from
      GOAL_METADATA. NEVER open a new board. Template:\n
      import pcbnew\n
      b=pcbnew.GetBoard()\n
      [b.Remove(x) for x in list(b.GetFootprints())]\n
      [b.Remove(t) for t in list(b.GetTracks())]\n
      [b.Remove(s) for s in list(b.GetDrawings())]\n
      def P(ref, fpid, x, y, rot=0, back=False):\n
      \\ttry:\n
      \\t\\tlib,name = fpid.split(':',1) if ':' in fpid else ('',fpid)\n
      \\t\\tfp = pcbnew.FootprintLoad('/usr/share/kicad/footprints/'+lib+'.pretty', name)\n
      \\t\\tif fp is None: return\n
      \\t\\tfp.SetReference(ref); fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x),pcbnew.FromMM(y)))\n
      \\t\\tfp.SetOrientationDegrees(rot)\n
      \\t\\tif back: fp.SetLayerAndFlip(b.GetLayerID('B.Cu'))\n
      \\t\\tb.Add(fp)\n
      \\texcept Exception: return  # custom/project libs aren't installed — skip\n
      def OUT(w,h):\n
      \\timport pcbnew as _p\n
      \\tfor a,c in [((0,0),(w,0)),((w,0),(w,h)),((w,h),(0,h)),((0,h),(0,0))]:\n
      \\t\\ts=_p.PCB_SHAPE(b); s.SetShape(_p.SHAPE_T_SEGMENT); s.SetStart(_p.VECTOR2I(_p.FromMM(a[0]),_p.FromMM(a[1]))); s.SetEnd(_p.VECTOR2I(_p.FromMM(c[0]),_p.FromMM(c[1]))); s.SetLayer(b.GetLayerID('Edge.Cuts')); b.Add(s)\n
      def N(name):\n
      \\ttry:\n
      \\t\\tif not b.FindNet(name):\n
      \\t\\t\\tb.Add(pcbnew.NETINFO_ITEM(b, name))\n
      \\t\\tnet_obj = b.FindNet(name)\n
      \\t\\tfor fp in b.GetFootprints():\n
      \\t\\t\\tfor pad in fp.Pads():\n
      \\t\\t\\t\\tif pad.GetNetCode() == 0:\n
      \\t\\t\\t\\t\\tpad.SetNet(net_obj); return\n
      \\texcept Exception: return\n
      OUT(<board_w>, <board_h>)\n
      P('R1','Resistor_SMD:R_0805_2012Metric', 10, 15, 0)\n
      ... one P(...) call per footprint, using the EXACT FPID 'name' and 'at'/'rot'
      from GOAL_METADATA's PER-FOOTPRINT PLACEMENT ...\n
      N('GND'); N('VCC'); ... one N('name') call for EVERY net listed in
      GOAL_METADATA's NETS table (do NOT skip nets; an empty net set scores 0 on
      the 25-pt connectivity component) ...\n
      pcbnew.Refresh()>",
  "validation_targets": { "footprint_count": <int>, "net_count": <int>,
                          "layer_count": <int>, "outline_bbox_mm": [w, h] },
  "fallback": "<simplest acceptable approximation: outline + footprints placed,
               no routing + est. score>"
}

The `full_code` MUST be a valid, exec'able multi-line program. Emit ONE P(...)
call for EVERY footprint in GOAL_METADATA's PER-FOOTPRINT PLACEMENT, passing its
exact FPID (the 'name' field, e.g. 'Resistor_SMD:R_0805_2012Metric'), 'at' x/y,
and 'rot'. Do not abbreviate the list or leave footprints at the origin.

ALSO emit ONE N('name') call for EVERY net listed in GOAL_METADATA's NETS field
(or, when only a count is given, generate plausible names like 'NET_001'..). The
connectivity score is worth 25 points; skipping nets locks the board to a 70-pt
ceiling. Track routing is NOT required (its scoring contribution comes via
named-net presence, which the N() helper covers).
"""


# --- compositional mode -----------------------------------------------------
# Appended to the system prompt when the build must be shown component-by-
# component (for training videos). Each step's `code` becomes an individually
# runnable, immediately-visible statement; the runner replays them one per turn.

_COMPOSITIONAL_FC = """

CRITICAL OUTPUT RULE: in COMPOSITIONAL mode, `steps[].code` is THE thing that
runs — it MUST be a non-empty, runnable one-liner for EVERY step. `full_code` is
secondary/reference. A plan with any empty step `code` is INVALID and useless.

COMPOSITIONAL MODE (IMPORTANT — overrides how `steps[].code` is written).
Show the build in FINE GRADATION: exactly one visible change per step. The
FreeCAD console namespace PERSISTS across steps. Pick the decomposition by
shape_class — and match the geometric fidelity of a one-shot build:

(A) ASSEMBLY (multiple distinct parts, e.g. chair = seat + back + leg x4):
    ONE component per step, each added as its OWN named object + recompute:
      step 1: import Part,FreeCAD as App; doc=App.ActiveDocument or App.newDocument(); [doc.removeObject(o.Name) for o in list(doc.Objects)]; seat=Part.makeBox(W,D,t); seat.translate(App.Vector(x,y,z)); o=doc.addObject('Part::Feature','seat'); o.Shape=seat; doc.recompute()
      step k: leg=Part.makeBox(a,b,h); leg.translate(App.Vector(...)); o=doc.addObject('Part::Feature','leg_k'); o.Shape=leg; doc.recompute()
    A single component MAY itself be a boolean (e.g. panel.cut(hole)) added as
    one object. Do NOT fuse ACROSS components — keep them separate.

(B) SINGLE_SOLID / REVOLVE / BOOLEAN (one connected part, e.g. screw, pipe bend,
    faucet): emit a FEATURE SEQUENCE — a base shape, then ordered effects, each
    step MUTATING the same solid `s` and updating its object so the shape VISIBLY
    EVOLVES (this is how you keep one-shot fidelity, not a coarse box):
      step 1: import Part,FreeCAD as App; doc=App.ActiveDocument or App.newDocument(); [doc.removeObject(o.Name) for o in list(doc.Objects)]; s=Part.makeCylinder(r,h); o=doc.addObject('Part::Feature','NAME'); o.Shape=s; doc.recompute()
      step 2 (add):    s=s.fuse(Part.makeCylinder(R,hd)); doc.getObject('NAME').Shape=s; doc.recompute()
      step 3 (cut):    c=Part.makeBox(...); c.translate(App.Vector(...)); s=s.cut(c); doc.getObject('NAME').Shape=s; doc.recompute()
      step 4 (fillet): s=s.makeFillet(rad,[s.Edges[i] for i in (0,1)]); doc.getObject('NAME').Shape=s; doc.recompute()
    Use REAL feature ops: fuse / cut / common / makeFillet / makeChamfer /
    makeRevolution. Same final solid as a one-shot build, just split into steps.

Rules for ALL steps:
- ONE physical line; reuse the persistent doc/Part/App; ALWAYS end with
  doc.recompute() so the change is visible.
- Produce 3–12 steps. NEVER produce zero steps. Every step's `code` must run on
  its own (given prior steps already ran).
- `full_code` = concatenation of all step codes (for reference).
"""

_COMPOSITIONAL_BL = """

CRITICAL OUTPUT RULE: in COMPOSITIONAL mode, `steps[].code` is THE thing that
runs — it MUST be a non-empty, runnable one-liner for EVERY step. `full_code` is
secondary. A plan with any empty step `code` is INVALID.

COMPOSITIONAL MODE (IMPORTANT — overrides how `steps[].code` is written).
Show the build in FINE GRADATION: one visible change per step. The Blender
console namespace PERSISTS across steps.
- step 1's `code` clears the scene once, then adds the first object:
    import bpy; bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(); bpy.ops.mesh.primitive_cube_add(size=s, location=(x,y,z)); o=bpy.context.active_object; o.scale=(..); o.name='base'
- ASSEMBLY (multiple objects, e.g. table = top + leg x4 / arrays): one object per
  step at its own location; later steps reuse the persistent namespace. For
  arrays a single list-comprehension step that adds the whole row is acceptable.
- SINGLE OBJECT with shaping (e.g. a beveled/boolean solid): step 1 adds the base
  primitive; later steps apply ONE effect to bpy.context.active_object and APPLY
  it (e.g. add+apply a Bevel or Boolean modifier with a cutter), so the shape
  visibly evolves. A single plain primitive (sphere/monkey/torus) = ONE step.
- NEVER produce zero steps. `full_code` = concatenation of all step codes.
"""


# --- Stage 2: decompose a known-good full_code into visible steps ------------

_DECOMPOSE_FC = """\
You are given a COMPLETE FreeCAD Part-API build (one line of Python that
reconstructs an asset). Split it into an ORDERED sequence of runnable steps that
build the SAME final solid INCREMENTALLY, so each step's change is visible in the
3D view (this is for a step-by-step construction video).

The FreeCAD console keeps its namespace across steps (variables, imports, `doc`
persist). Rules:
- STEP 1's code MUST initialise: import Part,FreeCAD as App; doc=App.ActiveDocument or App.newDocument(); [doc.removeObject(o.Name) for o in list(doc.Objects)]; then build + ADD the first piece and doc.recompute().
- EVERY step's code ends with doc.recompute() so its change shows; ONE physical line.
- ADDITIVE build (parts combined with makeCompound/fuse at the end): emit ONE
  part per step, each added as its OWN named object (doc.addObject per part) — do
  NOT wait for a final compound; each part appears as built.
- FEATURE build (one solid shaped by cut/fuse/fillet/chamfer/revolve): step 1 =
  base solid added as object 'NAME'; each later step applies ONE op to the solid
  and updates doc.getObject('NAME').Shape = s; doc.recompute(). The shape evolves.
- Use the EXACT dimensions, positions and ops from the input build — the final
  geometry MUST be identical. Do NOT simplify or drop parts/features.
- 2–15 steps. EVERY step `code` must be non-empty and run on its own (given prior
  steps ran).

Output JSON: {"steps":[{"i":1,"name":"...","code":"...","why":"..."}, ...]}
"""

_DECOMPOSE_BL = """\
You are given a COMPLETE Blender bpy build (one line). Split it into an ORDERED
sequence of runnable steps that build the SAME scene INCREMENTALLY (for a
step-by-step video). The console namespace persists across steps.
- STEP 1's code clears the scene then adds the first object:
    import bpy; bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete(); <first object>
- ADDITIVE (multiple parts): ONE part per step at its own location (a single
  list-comprehension/array step per logical group is OK). A part MAY itself be a
  base primitive plus the modifier(s) that shape it, added+APPLIED within that step.
- SINGLE shaped object: base primitive in step 1; each later step applies ONE
  effect to bpy.context.active_object and MUST APPLY it so geometry is real:
  a scale, OR add+apply a modifier (SUBSURF/MIRROR/SOLIDIFY/BEVEL/SCREW/ARRAY/
  BOOLEAN via o.modifiers.new(...) then bpy.ops.object.modifier_apply(modifier=...)),
  OR a curve add+convert, OR an edit-mode/bmesh detail pass.
- Preserve EVERY modifier and APPLY call from the input — do not drop modifiers
  when splitting (they are what make the shape, not just the base primitive).
- Use the EXACT params from the input; final scene geometry MUST be identical.
- 2–15 steps; EVERY step `code` non-empty + runnable.
Output JSON: {"steps":[{"i":1,"name":"...","code":"...","why":"..."}, ...]}
"""

_DECOMPOSE_KICAD = """\
You are given a COMPLETE KiCad pcbnew build (one line of Python that reconstructs
a board). Split it into an ORDERED sequence of runnable steps that build the SAME
board INCREMENTALLY, so each step's change is visible on the canvas (for a
step-by-step construction video). The pcbnew Scripting Console keeps its
namespace across steps (variables, imports, the board handle persist).
- STEP 1's code MUST initialise + clear the open board, then draw the board
  outline: import pcbnew; b=pcbnew.GetBoard(); [b.Remove(x) for x in list(b.GetFootprints())]; [b.Remove(t) for t in list(b.GetTracks())]; <draw Edge.Cuts outline>; pcbnew.Refresh().
- EVERY step ends with pcbnew.Refresh() so its change shows; ONE physical line.
- Build order across steps: outline -> ONE footprint per step (each at its own
  SetPosition) -> add nets -> route tracks (a single net's tracks may be one
  step) -> pour zones. Do NOT leave footprints at the origin.
- Use the EXACT positions, layers, nets and ops from the input build — the final
  board MUST be identical. Do NOT simplify or drop footprints/tracks.
- 2–15 steps; EVERY step `code` non-empty + runnable on its own (given prior
  steps ran).
Output JSON: {"steps":[{"i":1,"name":"...","code":"...","why":"..."}, ...]}
"""


def load_goal_metadata(goal_png: Path) -> dict[str, Any] | None:
    """Read the `<goal>.meta.json` sidecar next to the goal image."""
    sidecar = Path(str(goal_png)[: -len(goal_png.suffix)] + ".meta.json")
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return None


def _metadata_text(meta: dict[str, Any]) -> str:
    """Compact GOAL_METADATA block for the planner (bbox, parts, taxonomy)."""
    lines = ["GOAL_METADATA (trust these numbers):"]
    if meta.get("bbox_mm"):
        lines.append(f"  bbox_mm: {meta['bbox_mm']}")
    if meta.get("object_count") is not None:
        lines.append(f"  object_count: {meta['object_count']}")
    tax = (meta.get("surface_taxonomy") or {}).get("counts")
    if tax:
        lines.append(f"  surface_taxonomy: {tax}")
    parts = meta.get("parts") or []
    if parts:
        lines.append(f"  PER-PART DECOMPOSITION ({len(parts)} parts, by volume):")
        for i, p in enumerate(parts):
            bb = p.get("bbox") or p.get("dims")
            org = p.get("origin")
            lines.append(f"    part_{i:02d}: bbox={bb} origin={org}")
    # KiCad: surface the board's footprint table + net/layer counts so the
    # planner inherits exact placements (analogous to the FC per-part table).
    kc = meta.get("kicad") or {}
    if kc:
        lines.append(
            f"  PCB: footprints={kc.get('footprint_count')} nets={kc.get('net_count')} "
            f"layers={kc.get('layer_count')} outline_bbox_mm={kc.get('outline_bbox_mm')}")
        fps = kc.get("footprints") or []
        # Fix #4 (panelization-aware planning): TESTED + REJECTED on 2026-06-12.
        # Two variants tried on 16 panel-detected boards (paired vs fix#1):
        #   #4a "iterate" hint:   mean Δ -4.4 (SparkFun_GNSS 32->0, PointController 77->60)
        #   #4b "faithful" hint:  mean Δ -2.0 (urchin 49->0, +20 SparkFun_GNSS, +6.7 PointController)
        # In both, the wins on a few panels were swamped by a single planner
        # failure caused by the expanded prompt structure. The default code
        # below (which matches pre-#4 behavior) is the version we ship; the
        # detection logic remains in place as a no-op so a future, safer
        # template (e.g. mid-board sub-table with strict format) can flip the
        # flag without re-locating the call site.
        # Decomposition-aware grounding (when a precomputed subcircuit
        # decomposition is present, surface clusters BEFORE the flat list so
        # the planner thinks regionally — the way board engineers do — and
        # large flat tables are easier to walk through cluster-by-cluster
        # instead of all-at-once).
        decomp = kc.get("decomposition")
        # SUBCIRCUIT CLUSTERS block is INERT at temp=0 across every model tier
        # tested (flash@low Δ=0.00 paired, flash@medium 0.00, pro@low 0.00 — see
        # modelscan_*.jsonl 2026-06-12). Default OFF; flip KICAD_DECOMP_ENABLED=1
        # only to re-test (e.g. with reasoning-required builds or a future system
        # prompt that mandates cluster-organised output). Sidecars + manifests
        # remain in place for downstream uses (per-cluster reconstruction).
        if os.environ.get("KICAD_DECOMP_ENABLED", "0") != "1":
            decomp = None
        if decomp and decomp.get("clusters"):
            cs = decomp["clusters"]
            lines.append(f"")
            lines.append(f"  SUBCIRCUIT CLUSTERS ({len(cs)} groups, "
                         f"identified by net+anchor clustering):")
            for ci, c in enumerate(cs):
                bb = c.get("bbox") or {}
                keys = c.get("key_refs") or []
                lines.append(
                    f"    [#{ci:02d} {c.get('label','?'):<12}] "
                    f"{c.get('footprint_count')} fp at "
                    f"({bb.get('cx',0):.0f},{bb.get('cy',0):.0f}) "
                    f"{bb.get('w',0):.0f}x{bb.get('h',0):.0f}mm  "
                    f"keys=[{','.join(keys[:3])}]")
        if fps:
            lines.append(f"  PER-FOOTPRINT PLACEMENT ({len(fps)} footprints):")
            for f in fps:
                lines.append(
                    f"    {f.get('ref')}: at={f.get('at')} rot={f.get('rot')} "
                    f"layer={f.get('layer')} ({f.get('name')})")
        nets = kc.get("nets") or []
        if nets:
            lines.append(f"  NETS: {nets}")
    return "\n".join(lines)


class FrontierPlanner:
    def __init__(self, api_key: str, model: str = DEFAULT_PLANNER_MODEL,
                 app: str = "freecad",
                 image_max_dim: int = 1024, timeout: float = 240.0,
                 reasoning_effort: str = "low", max_tokens: int = 24000,
                 compositional: bool = False,
                 referer: str = "https://cua-smoketest.local",
                 title: str = "cua-smoketest-planner"):
        if not api_key:
            raise ValueError("FrontierPlanner needs an OpenRouter api_key")
        self.api_key = api_key
        self.model = model
        self.app = app
        # compositional: each step's `code` must be an INDIVIDUALLY runnable +
        # visible statement (adds one component to the doc + recomputes), so the
        # runner can replay them one-per-turn for a fine-grained build video.
        self.compositional = compositional
        self.image_max_dim = image_max_dim
        self.timeout = timeout
        # Gemini 3.x Pro is a heavy reasoner: at high effort it burned ~14k
        # reasoning tokens and truncated the JSON (finish_reason=length) under an
        # 8k cap, at ~$0.20/call. "low" effort + a generous output budget keeps
        # the plan complete and cuts cost ~5x.
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens
        self.referer = referer
        self.title = title

    # --- public API ---------------------------------------------------------

    def _plan_once(self, goal_png: Path, goal_name: str | None = None,
                   style_hint: str = "") -> dict[str, Any] | None:
        """Stage 1 only: produce a validated BUILD_PLAN with full_code (no
        decompose). `style_hint` is an extra directive appended to the user
        prompt (used by best-of-both to bias primitive vs enhanced builds)."""
        meta = load_goal_metadata(goal_png) or {}
        user_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": "GOAL views:"},
            self._image_block(goal_png),
            {"type": "text", "text": _metadata_text(meta)},
        ]
        if goal_name:
            user_blocks.append({"type": "text",
                                "text": f"GOAL_NAME = '{goal_name}' (name the top object this)"})
        if style_hint:
            user_blocks.append({"type": "text", "text": style_hint})
        user_blocks.append({"type": "text",
                            "text": "Output the BUILD_PLAN JSON now."})
        system = {"blender": _PLANNER_SYSTEM_BL,
                  "kicad": _PLANNER_SYSTEM_KICAD}.get(self.app, _PLANNER_SYSTEM)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_blocks},
            ],
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
            "reasoning": {"effort": self.reasoning_effort},
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.referer,
            "X-Title": self.title,
        }
        try:
            body = self._post_with_retry(headers, payload)
            choice = body["choices"][0]["message"]
            content = choice.get("content") or ""
            reasoning = choice.get("reasoning")  # frontier model's thinking trace
            plan = _extract_json(content)
        except Exception as exc:  # noqa: BLE001 — planning is best-effort
            print(f"[planner] FAILED: {type(exc).__name__}: {exc}", flush=True)
            return None
        if not _validate_plan(plan):
            print(f"[planner] invalid plan schema: {str(plan)[:200]}", flush=True)
            return None
        # Stash planner provenance so the runner persists it in build_plan.json:
        # usage/cost (for the cost ablation) + the reasoning trace (for audit).
        plan["_planner_model"] = self.model
        plan["_planner_usage"] = body.get("usage")
        plan["_planner_reasoning"] = reasoning
        return plan

    def _decompose_into(self, plan: dict[str, Any], goal_name: str | None) -> dict[str, Any]:
        """Stage 2: decompose plan.full_code into ordered visible steps in-place."""
        if self.compositional and plan.get("full_code"):
            dsteps, dusage = self._decompose(plan["full_code"], goal_name)
            if dsteps:
                plan["steps"] = dsteps
                plan["_decompose_usage"] = dusage
                plan["_decomposed"] = True
            else:
                print("[planner] decompose failed — keeping full_code as 1 step", flush=True)
        return plan

    def plan(self, goal_png: Path, goal_name: str | None = None) -> dict[str, Any] | None:
        """Produce a BUILD_PLAN for the goal (stage 1 + stage-2 decompose when
        compositional). Returns the validated plan dict, or None on failure."""
        plan = self._plan_once(goal_png, goal_name)
        if plan is None:
            return None
        return self._decompose_into(plan, goal_name)

    # Best-of-both: generate a primitive-biased AND an enhanced-biased candidate,
    # score each by headless Chamfer vs the goal asset, keep the better. Captures
    # the enhanced toolkit's wins (organic/vessel) without its losses on shapes a
    # clean primitive build already nails. Falls back to the primitive candidate
    # when scoring is inconclusive (never worse than the proven baseline).
    _BOB_HINTS = [
        ("primitive",
         "STYLE CONSTRAINT: Use ONLY clean primitives placed and scaled to the goal "
         "(cube/cylinder/uv_sphere/cone/torus + scale + array comprehensions). Do NOT "
         "use SUBSURF/SCREW/SOLIDIFY/BEVEL/MIRROR/BOOLEAN/curves/bmesh. Prefer the "
         "simplest correct construction at the GOAL bbox scale."),
        ("enhanced",
         "STYLE CONSTRAINT: Use the ENHANCE FIDELITY toolkit (SUBSURF/SCREW/SOLIDIFY/"
         "BEVEL/MIRROR/curves) wherever the FORM clearly benefits, applying every "
         "modifier, at the GOAL bbox scale."),
    ]

    def plan_best_of(self, goal_png: Path, goal_name: str | None = None,
                     goal_asset: str | None = None, blender_bin: str | None = None,
                     candidates: int = 2) -> dict[str, Any] | None:
        """Generate `candidates` style-biased plans, score each by Chamfer vs the
        goal asset, decompose + return the winner. Needs goal_asset + blender_bin
        to score; if unavailable, falls back to plain plan()."""
        if self.app != "blender" or not goal_asset or not blender_bin or not Path(goal_asset).exists():
            return self.plan(goal_png, goal_name)
        hints = self._BOB_HINTS[:max(1, candidates)]
        cands = []
        for label, hint in hints:
            p = self._plan_once(goal_png, goal_name, style_hint=hint)
            if p and p.get("full_code"):
                sc = _score_candidate(p["full_code"], goal_asset, blender_bin)
                cands.append((label, sc, p))
                print(f"[best-of-both] candidate '{label}' chamfer={sc}", flush=True)
        if not cands:
            return self.plan(goal_png, goal_name)
        # pick highest score; tie/None-safe; on equal scores prefer 'primitive'
        order = {"primitive": 0, "enhanced": 1}
        cands.sort(key=lambda t: (-(t[1] or 0.0), order.get(t[0], 9)))
        win_label, win_score, win = cands[0]
        win["_bob_winner"] = win_label
        win["_bob_scores"] = {l: s for l, s, _ in cands}
        print(f"[best-of-both] winner='{win_label}' (score={win_score}) "
              f"of {[(l, s) for l, s, _ in cands]}", flush=True)
        return self._decompose_into(win, goal_name)

    def _decompose(self, full_code: str, goal_name: str | None):
        """Stage 2: split a complete build into ordered runnable+visible steps
        whose concatenation reproduces the SAME final geometry. Returns
        (steps_list, usage) or ([], None)."""
        system = {"blender": _DECOMPOSE_BL,
                  "kicad": _DECOMPOSE_KICAD}.get(self.app, _DECOMPOSE_FC)
        user = f"GOAL_NAME = {goal_name!r}\n\nFULL BUILD to decompose:\n{full_code}\n\nOutput the decomposition JSON now."
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.0, "max_tokens": self.max_tokens,
            "reasoning": {"effort": "medium"},  # decomposition benefits from reasoning
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json",
                   "HTTP-Referer": self.referer, "X-Title": self.title}
        try:
            body = self._post_with_retry(headers, payload)
            obj = _extract_json(body["choices"][0]["message"].get("content") or "")
            steps = [s for s in (obj.get("steps") or []) if s.get("code")]
            return (steps if steps else []), body.get("usage")
        except Exception as exc:  # noqa: BLE001
            print(f"[planner] decompose error: {type(exc).__name__}: {exc}", flush=True)
            return [], None

    # --- internals ----------------------------------------------------------

    def _image_block(self, png_path: Path) -> dict[str, Any]:
        from PIL import Image  # local import
        import io
        img = Image.open(png_path)
        if max(img.size) > self.image_max_dim:
            img = img.copy()
            img.thumbnail((self.image_max_dim, self.image_max_dim), Image.LANCZOS)
            buf = io.BytesIO(); img.save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
        else:
            data = png_path.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        return {"type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"}}

    def _post_with_retry(self, headers: dict, payload: dict,
                         max_attempts: int = 6) -> dict:
        last = ""
        for attempt in range(1, max_attempts + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(_ENDPOINT, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code in (405, 408, 429) or resp.status_code >= 500:
                    last = f"HTTP {resp.status_code}: {resp.text[:200]}"
                elif resp.status_code != 200:
                    raise RuntimeError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:400]}")
                else:
                    body = resp.json()
                    if "error" in body and "choices" not in body:
                        raise RuntimeError(f"OpenRouter error: {body['error']}")
                    return body
            time.sleep(min(2 ** attempt, 30))
        raise RuntimeError(f"planner POST failed after {max_attempts} attempts: {last}")


# --- plan validation + rendering --------------------------------------------

def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # last-ditch 1: first {...} span (strict json)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    # last-ditch 2: json_repair on the (possibly fenced) text. Handles the
    # common KiCad-planner failure mode: gemini emits Python inside `full_code`
    # with unescaped quotes/backslashes/newlines that strict json.loads chokes
    # on at chars 1-3k. Empirically ~15-30% of board×planner cells in the
    # modelscan (2026-06-12) hit this and dropped to score=None. json_repair
    # silently fixes the most frequent cases (unterminated strings, missing
    # commas, smart quotes). It returns "" when unfixable — coerce to a hard
    # parse failure in that case so callers see it as before.
    try:
        from json_repair import repair_json  # type: ignore[import-not-found]  # noqa: PLC0415
        candidate = m.group(0) if m else text
        repaired = repair_json(candidate, return_objects=True)
        if isinstance(repaired, dict) and repaired:
            return repaired
    except Exception:  # noqa: BLE001
        pass
    # Re-raise the original strict-loads error so the caller logs the precise
    # location (used for triage of regressions).
    return json.loads(text)  # raises JSONDecodeError


def _validate_plan(plan: Any) -> bool:
    if not isinstance(plan, dict):
        return False
    fc = plan.get("full_code")
    if not isinstance(fc, str) or len(fc) < 20 or not (
            "doc" in fc or "bpy" in fc or "pcbnew" in fc or "GetBoard" in fc):
        return False
    if not isinstance(plan.get("steps"), list) or not plan["steps"]:
        return False
    return True


def render_plan_block(plan: dict[str, Any], plan_format: str = "python_eval") -> str:
    """Render the BUILD_PLAN guidance block injected into the SLM prompt.

    plan_format:
      - "python_eval": surface the ready full_code reconstruction (default).
      - "build_star": surface the per-step build_* ops (dims/origin) instead.
    """
    p = plan
    lines = [
        "BUILD_PLAN (authoritative guidance from a frontier planner — follow it):",
        f"  brief: {p.get('brief','')}",
        f"  shape_class: {p.get('shape_class','')}",
    ]
    if p.get("parameters"):
        lines.append(f"  parameters: {p['parameters']}")
    vt = p.get("validation_targets") or {}
    if vt:
        lines.append(f"  target object_count={vt.get('object_count')} bbox_mm={vt.get('bbox_mm')}")
    lines.append("  steps:")
    for s in p.get("steps", []):
        if plan_format == "build_star" and s.get("op"):
            lines.append(f"    {s.get('i')}. {s['op']} name={s.get('name')} "
                         f"dims={s.get('dims')} origin={s.get('origin')}  # {s.get('why','')}")
        else:
            lines.append(f"    {s.get('i')}. {s.get('name')}: {s.get('why','')}")

    if plan_format == "python_eval":
        # Verb-adapt: KiCad full_code is pcbnew (emitted via a pcbnew_eval
        # action), FreeCAD/Blender via python_eval. Detect from the code.
        fc = p.get("full_code", "")
        verb = "pcbnew_eval" if ("pcbnew" in fc or "GetBoard" in fc) else "python_eval"
        target = "board" if verb == "pcbnew_eval" else "asset"
        lines += [
            "",
            f"DO THIS: emit the {verb} below ONCE to build the whole {target}. "
            "It is idempotent (reuses+clears the open document/board), so it "
            "already creates the full result in a single shot. After it runs, your "
            'NEXT action MUST be {"type":"terminate"} — do NOT re-emit it. '
            "Re-running the same code wastes steps and will trigger loop-kill. "
            "Only emit DIFFERENT code if AGENT_STATE shows the build genuinely "
            "failed or is wrong.",
            f"  {fc}",
        ]
    else:  # build_star
        lines += [
            "",
            "Emit one build_* action per step above, in order (with the listed "
            "dims AND origin), then compound and terminate. Do NOT leave parts at "
            "the origin.",
        ]
    if p.get("fallback"):
        lines.append(f"\n  fallback if stuck: {p['fallback']}")
    return "\n".join(lines) + "\n"
