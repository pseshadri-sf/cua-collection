"""OpenRouter VLM client for the agentic trajectory harness.

Sends the goal-state image, the current-state screenshot, and a system
prompt to a vision-language model; returns the parsed JSON action and
the model's reasoning trace.

Thinking mode: enabled via OpenRouter's `reasoning` parameter for models
that support it (google/gemma-4-31b-it advertises `reasoning` in its
supported_parameters). Reasoning content is returned in the
`reasoning` field of the assistant message and surfaced as the
trajectory `rationale` when present.
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


def _extract_goal_name(goal_png: Path) -> str | None:
    """Parse the asset stem from a goal screenshot filename.

    Goal screenshots follow `<PREFIX>_NN_loaded_<stem>_<ext>.png` for FreeCAD
    or `<PREFIX>_NN_loaded_<numeric_stem>.png` for Blender. We extract the
    middle part — that's the asset name the geometric evaluator credits in
    `name_overlap`. Returns None on no match.
    """
    name = goal_png.name
    # FreeCAD pattern: stem then explicit extension token
    m = re.match(r"^[A-Z]_\d+_loaded_(.+?)_(?:step|stp|fcstd|brep|iges|igs|stl)\.png$",
                 name, re.IGNORECASE)
    if m:
        return m.group(1).split("__")[-1]  # take final segment of cat__cat__name
    # Blender pattern: NN_name_with_underscores
    m = re.match(r"^[A-Z]_\d+_loaded_\d+_([a-z_0-9]+)\.png$", name, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


# Per-turn schema reminder injected only when the active model is Qwen3-VL.
# Empirically, Qwen3-VL-30B/32B-Instruct on this pipeline (a) emits
# `{"type":"click","x":[X,Y]}` for clicks (the parse layer normalizes
# this, but emitting the right form saves a coercion round-trip), and
# (b) ignores the system-prompt's Strategy B even though it's spelled
# out — defaulting to repeated clicks on the same coord. We inject the
# strategy into the per-turn user message because Qwen weights recent
# user content more than the system prompt. The reminder is now
# app-aware: FreeCAD agents see FC Python-console Strategy B, Blender
# agents see bpy-equivalent Strategy B.
_QWEN_SCHEMA_REMINDER_COMMON = """\
STRICT SCHEMA REMINDER:

  click / move_to / double_click / right_click:
    ✓ correct: {"type":"click","x":231,"y":308}
    ✗ wrong  : {"type":"click","x":[231,308]}
    ✗ wrong  : {"type":"click","coords":[231,308]}
    "x" and "y" are SEPARATE integer fields. Never a list, tuple, or
    nested object. Never wrap them in "position", "point", or "coords".

  hotkey: {"type":"hotkey","keys":["ctrl","o"]}   — list of strings
  type:   {"type":"type","text":"..."}             — string under "text"
  key:    {"type":"key","key":"enter"}             — string under "key"

DIMENSION ESTIMATION (CRITICAL): The templates below show DEFAULT
numbers. Before typing, look carefully at the GOAL_STATE image and
adjust the numbers so the proportions match what you see. Read the
image: which axis is longest, which is shortest, is the shape square
in plan view, is it a thin sheet (one dim ≪ the others) or a chunky
block (all dims comparable). Type numbers that match the goal — do
NOT copy the template verbatim.

ANTI-REPETITION RULE: Look at Recent history. If your previous
action was identical (or nearly identical) to the action you are
about to emit AND the CURRENT_STATE did not change visibly, pick a
DIFFERENT action.
  - Repeated identical click coords → the dropdown auto-closed.
    Use a compound macro instead.
  - Repeated identical `type` / `python_eval` payload → your code
    didn't take effect (focus was lost, or the console swallowed it).
    Either issue {"type":"focus_viewport"} then re-attempt, OR
    type a DIFFERENT variant — e.g. wrap in
    `exec(...)` / `eval(...)` / append `;bpy.context.view_layer.update()`
    / change at least one numeric parameter. Re-typing the SAME bytes
    a third time is wasted budget.
  - Repeated identical `menu_navigate` path → that path either failed
    or already executed. Switch to typing instead.

FORBIDDEN: File>Open, File>Recent, drag-and-drop. The goal is to
CONSTRUCT the geometry — never load it.

ZOOM-OUT RULE (CRITICAL — applies after every build/python_eval):
After ANY action that adds or modifies geometry, look carefully at
CURRENT_STATE. If the rendered geometry is:
  - invisible / not in frame
  - cut off at the viewport edges
  - filling less than ~15% of the viewport (a tiny dot in a sea of
    dark gray)
  - centred at the world origin but the camera is still on the
    default position
your NEXT action MUST be {"type":"frame_view"} (FreeCAD) or
{"type":"frame_all"} (Blender). Do NOT emit another python_eval,
build_*, terminate, or anything else until you can see the geometry
clearly in the viewport. A geometrically-correct python_eval that
produces invisible geometry scores 0 because we cannot verify it.

Exception: do not emit frame_view/frame_all TWICE in a row — if you
just did one and the geometry still isn't visible, the issue is the
geometry itself (wrong scale, off-axis, etc.) — re-emit a corrected
python_eval / build_*.

Each turn emits exactly ONE action wrapped as:
  {"action": <action object>, "rationale": "<one or two sentences>"}
"""

_QWEN_FREECAD_STRATEGY = """\

REQUIRED STRATEGY (FreeCAD): Reconstruct GOAL_STATE in three atomic
compound actions — use these macros, NOT the underlying 4-step
chains, to save VLM round-trips and avoid focus-loss failures:

  1. {"type":"python_eval","code":"<one-line Python that builds the goal>"}
        — Atomic: opens the Python console (idempotent), focuses its
          input field, types `code`, presses Enter. Replaces the
          (menu_navigate + click + type + key) prelude with ONE action.
          Single-line code only; no \\n.
  2. {"type":"frame_view"}
        — Atomic: focuses viewport, switches to isometric (key "0"),
          fit-all (keys "v","f"). Replaces (focus_viewport + 3 keys).
  3. {"type":"terminate"}
        — when CURRENT_STATE matches GOAL_STATE.

BEFORE TYPING (dimension estimate — MANDATORY): In your `rationale`
field for the python_eval action, FIRST write your estimate of the
goal's bounding box in millimetres, e.g. `approx bbox W×D×H =
2000×1200×100 mm (a wide flat slab)`. Then build the python_eval
template using EXACTLY those numbers. The template defaults shown
below are starting points only — typing them verbatim almost always
fails because the goal is at a different scale.

NAMING (free 10 points): The geometric evaluator credits Jaccard
overlap between your object name and the goal's object names. Use
the GOAL_NAME passed to you in the user prompt as the third arg to
addObject, e.g. `doc.addObject('Part::Feature','GOAL_NAME')`. Do
NOT use generic names like 'Box', 'Plate', 'Cyl'.

SELF-VERIFICATION + ITERATION: After your first python_eval,
compare CURRENT_STATE to GOAL_STATE. If the geometry's proportions
or scale look visibly wrong (e.g. you built a 100mm box but the goal
is a 2000mm door), emit a SECOND python_eval with adjusted numbers.
The console persists state — you can overwrite the object. Iteration
is ENCOURAGED: agents that iterate score on average +6.6 pts higher
than agents that type once and quit.

Python templates (adjust the numbers per the goal image):

FreeCAD dimension hints (millimetres):
  - door/window panel: thickness 20-50, height 2000-2200, width 800-1200
  - bracket/small plate: 20-200 mm
  - furniture component: 300-1500 mm
  - architectural slab / shower pad: 1000-3000 mm

  cylinder (round bar / disk / pipe — adjust radius R and height H):
    doc=App.newDocument();import Part;c=Part.makeCylinder(R,H);o=doc.addObject('Part::Feature','Cyl');o.Shape=c;doc.recompute()

  box / plate / door panel (rectangular slab — adjust X,Y,Z to goal):
    doc=App.newDocument();import Part;b=Part.makeBox(X,Y,Z);o=doc.addObject('Part::Feature','Plate');o.Shape=b;doc.recompute()

  bracket with a hole (adjust X,Y,Z; HX,HY,HR position the hole):
    doc=App.newDocument();import Part;b=Part.makeBox(X,Y,Z);h=Part.makeCylinder(HR,Z,App.Vector(HX,HY,0),App.Vector(0,0,1));s=b.cut(h);o=doc.addObject('Part::Feature','Br');o.Shape=s;doc.recompute()

  L-shaped bracket (horizontal base + vertical wall):
    doc=App.newDocument();import Part;b=Part.makeBox(X,Y,T);w=Part.makeBox(T,Y,H);br=b.fuse(w);o=doc.addObject('Part::Feature','L');o.Shape=br;doc.recompute()

  tray / shower pad / pan (box with shallow inset on top):
    doc=App.newDocument();import Part;o2=Part.makeBox(OX,OY,OZ);i=Part.makeBox(IX,IY,IZ);i.translate(App.Vector(WX,WY,OZ-IZ));s=o2.cut(i);o=doc.addObject('Part::Feature','Pad');o.Shape=s;doc.recompute()

  door with handle hole (panel + cylindrical hole, for door-style goals):
    doc=App.newDocument();import Part;p=Part.makeBox(W,T,H);hole=Part.makeCylinder(HR,T*2,App.Vector(W-100,T/2,H*0.5),App.Vector(0,1,0));s=p.cut(hole);o=doc.addObject('Part::Feature','Door');o.Shape=s;doc.recompute()

  sphere (for round/ball goals):
    doc=App.newDocument();import Part;s=Part.makeSphere(R);o=doc.addObject('Part::Feature','Sph');o.Shape=s;doc.recompute()

  fused multi-part assembly (combine two shapes):
    doc=App.newDocument();import Part;a=Part.makeBox(X1,Y1,Z1);b=Part.makeCylinder(R,H);b.translate(App.Vector(TX,TY,TZ));s=a.fuse(b);o=doc.addObject('Part::Feature','A');o.Shape=s;doc.recompute()

  pulley / disk with center bore (round flange + axial hole):
    doc=App.newDocument();import Part;disk=Part.makeCylinder(OR,T);bore=Part.makeCylinder(IR,T*2,App.Vector(0,0,-T/2));s=disk.cut(bore);o=doc.addObject('Part::Feature','Pulley');o.Shape=s;doc.recompute()

  bearing (concentric rings: outer race + inner race, race width T):
    doc=App.newDocument();import Part;outer=Part.makeCylinder(OR,T);mid=Part.makeCylinder(MR,T);inner=Part.makeCylinder(IR,T);s=outer.cut(mid).fuse(inner);o=doc.addObject('Part::Feature','Bearing');o.Shape=s;doc.recompute()

  sprocket (toothed disk — approximate as cylinder + small radial cubes for teeth):
    doc=App.newDocument();import Part,math;disk=Part.makeCylinder(R,T);teeth=disk;
    [teeth:=teeth.fuse(Part.makeBox(TW,TW,T,App.Vector(R*math.cos(2*math.pi*i/N)-TW/2,R*math.sin(2*math.pi*i/N)-TW/2,0))) for i in range(N)];
    o=doc.addObject('Part::Feature','Sprk');o.Shape=teeth;doc.recompute()
    (compress to one line; if too complex, fall back to plain pulley template with R+T tuned)

  ring / torus / round gasket:
    doc=App.newDocument();import Part;t=Part.makeTorus(MR,mR);o=doc.addObject('Part::Feature','Ring');o.Shape=t;doc.recompute()

  socket-head cap screw / bolt (head + shaft, simplified):
    doc=App.newDocument();import Part;head=Part.makeCylinder(HR,HT);shaft=Part.makeCylinder(SR,SL,App.Vector(0,0,HT));s=head.fuse(shaft);o=doc.addObject('Part::Feature','Bolt');o.Shape=s;doc.recompute()

NO-TEMPLATE FALLBACK: If none of the templates above clearly match
the goal shape, DO NOT loop on menu_navigate looking for a primitive
button — that's wasted budget. Type the closest approximation:
  - Anything roundish & symmetric  → cylinder or pulley template
  - Anything blocky / rectilinear  → box or fused multi-part
  - Anything organic / smooth      → sphere or torus
A rough approximation that runs is worth 30-60 match_score points;
a perfect navigation through menus with no `type` action is worth 0.

GUARDRAIL: Emit at most TWO `menu_navigate` actions per trajectory.
The only one you genuinely need is the Python-console open at step 1.
After that, every action should be a click/type/key/focus_viewport
that advances the reconstruction. If you find yourself about to emit
a third menu_navigate, type the closest-matching template instead.
"""

_QWEN_BLENDER_STRATEGY = """\

REQUIRED STRATEGY (Blender): Reconstruct GOAL_STATE by typing bpy
Python code into Blender's Scripting workspace text editor. Do NOT
spam clicks on the toolbar. The action sequence:

  1. {"type":"switch_workspace","name":"Scripting"}
        — switches to Blender's Scripting workspace (script editor
          on left, console below). If unsupported, fall back to:
        {"type":"menu_navigate","path":["Window","Workspace","Scripting"]}
  2. {"type":"click","x":300,"y":700}
        — focus the Python interactive console at the bottom.
  3. {"type":"type","text":"<one-line bpy code that builds the goal>"}
        — see templates below. Single line, semicolons to separate.
        REMEMBER: estimate dimensions from the goal image.
  4. {"type":"key","key":"enter"}        — executes.
  5. {"type":"focus_viewport"}           — focuses 3D viewport.
  6. {"type":"key","key":"numpad_5"} then {"type":"key","key":"numpad_0"}
        — orthographic + camera view if available, else key="0".
  7. {"type":"key","key":"home"}         — fit all (Blender's frame-all).
  8. {"type":"terminate"}                — when CURRENT matches GOAL.

bpy templates (adjust numbers per the goal image):

SELF-VERIFICATION + ITERATION: After your first python_eval,
compare CURRENT_STATE to GOAL_STATE. If wrong shape or scale,
issue a SECOND python_eval with corrections — variables persist
across calls. If you see N distinct objects in the goal but built
only one, type N primitive_*_add calls.

  cube (size = single edge length):
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.mesh.primitive_cube_add(size=2,location=(0,0,0))

  sphere (UV sphere; segments/ring controls smoothness):
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.mesh.primitive_uv_sphere_add(segments=48,ring_count=24,radius=1)

  icosphere:
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3,radius=1)

  cylinder:
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.mesh.primitive_cylinder_add(vertices=48,radius=1,depth=2)

  cone:
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.mesh.primitive_cone_add(vertices=48,radius1=1,radius2=0,depth=2)

  torus:
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.mesh.primitive_torus_add(major_radius=1.5,minor_radius=0.4)

  monkey (Suzanne head — for cartoon/face-like goals):
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.mesh.primitive_monkey_add(size=2)

  plane / grid:
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.mesh.primitive_grid_add(x_subdivisions=12,y_subdivisions=12,size=6)

  multiple objects in a row (count N, spacing S):
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete()
    Then issue N more `bpy.ops.mesh.primitive_cube_add(location=(i*S,0,0))` calls
    via N additional type+enter pairs, varying location.

  pyramid stack (M layers):
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete()
    Then for each layer issue: bpy.ops.mesh.primitive_cube_add(location=(0,0,i),scale=(M-i,M-i,1))

  ring of N objects (circular arrangement, radius R):
    import bpy,math;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();[(bpy.ops.mesh.primitive_uv_sphere_add(location=(R*math.cos(2*math.pi*i/N),R*math.sin(2*math.pi*i/N),0))) for i in range(N)]

  boolean DIFFERENCE (cube with a spherical bite taken out):
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.mesh.primitive_cube_add(size=2);c=bpy.context.active_object;bpy.ops.mesh.primitive_uv_sphere_add(radius=1.3,location=(1,1,1));s=bpy.context.active_object;s.hide_viewport=True;m=c.modifiers.new('b','BOOLEAN');m.operation='DIFFERENCE';m.object=s

  boolean UNION (multiple primitives merged into one mass):
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.mesh.primitive_cube_add(size=2);base=bpy.context.active_object
    Then for each extra shape: bpy.ops.mesh.primitive_cube_add(size=1.2,location=(X,Y,Z));ext=bpy.context.active_object;ext.hide_viewport=True;m=base.modifiers.new('u','BOOLEAN');m.operation='UNION';m.object=ext

  subdivision + bevel modifier stack (rounded organic cube):
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.mesh.primitive_cube_add(size=2);o=bpy.context.active_object;b=o.modifiers.new('b','BEVEL');b.width=0.25;b.segments=4;s=o.modifiers.new('s','SUBSURF');s.levels=3

  array modifier (N copies along an axis, spacing S):
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.mesh.primitive_cube_add(size=0.5);o=bpy.context.active_object;m=o.modifiers.new('a','ARRAY');m.fit_type='FIXED_COUNT';m.count=N;m.relative_offset_displace=(S,0,0)

  mirror modifier (symmetric copy across an axis — combine with array for ladders):
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.mesh.primitive_cube_add(size=0.5,location=(0.7,0,0));o=bpy.context.active_object;mr=o.modifiers.new('m','MIRROR');mr.use_axis=(True,False,False)

  text (3D extruded letters — for word/letter goals):
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.object.text_add(location=(-1.5,0,0));t=bpy.context.active_object;t.data.body='BLEND';t.data.extrude=0.15

  multi-object kitbash (robot/figure/assembly — N parts):
    Multiple type+enter pairs, each adding one part:
    bpy.ops.mesh.primitive_cube_add(size=2,location=(0,0,-0.5))     # base
    bpy.ops.mesh.primitive_cube_add(size=1.6,location=(0,0,1.0))    # torso
    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.55,location=(0,0,2.3))  # head
    bpy.ops.mesh.primitive_cylinder_add(radius=0.2,depth=1.6,location=(1.3,0,1.0))  # arm

  random scatter (N primitives at random positions — for messy scenes):
    import bpy,random;random.seed(42);bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();[bpy.ops.mesh.primitive_cube_add(size=random.uniform(0.2,0.5),location=(random.uniform(-2,2),random.uniform(-2,2),random.uniform(0,2))) for _ in range(N)]

  metaballs blob (smooth organic — for fluid/blob goals):
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.object.metaball_add(type='BALL',location=(-0.4,0,0.5));bpy.ops.object.metaball_add(type='BALL',location=(0.4,0,0.5))

  helix / spiral / spring (screw modifier on a profile):
    import bpy,math;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.mesh.primitive_plane_add(size=0.4,location=(1,0,0));o=bpy.context.active_object;o.rotation_euler=(math.radians(90),0,0);m=o.modifiers.new('s','SCREW');m.steps=32;m.screw_offset=4.0

  displaced plane / terrain (high-poly plane with noise displacement):
    import bpy;bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete();bpy.ops.mesh.primitive_grid_add(x_subdivisions=40,y_subdivisions=40,size=4);o=bpy.context.active_object;tex=bpy.data.textures.new('n',type='CLOUDS');m=o.modifiers.new('d','DISPLACE');m.texture=tex;m.strength=0.4

NO-TEMPLATE FALLBACK: If none of the templates above clearly match
the goal shape, DO NOT loop on menus. Type the closest-matching
template — a rough approximation is worth 30-60 match_score points;
a perfectly-navigated menu with no python_eval is worth 0.
"""

_QWEN_FREECAD_REMINDER = _QWEN_SCHEMA_REMINDER_COMMON + _QWEN_FREECAD_STRATEGY
_QWEN_BLENDER_REMINDER = _QWEN_SCHEMA_REMINDER_COMMON + _QWEN_BLENDER_STRATEGY

# Backward-compat alias for any external import — defaults to FreeCAD.
_QWEN_SCHEMA_REMINDER = _QWEN_FREECAD_REMINDER


# v2 structured actions — REPLACES the python_eval-templates strategy
# (rather than augmenting it). The v1 preamble was prepended on top of the
# existing strategy and was uniformly ignored by Qwen because the templates
# section right below it gave it dozens of python_eval examples to pattern-
# match on. v2 swaps the entire strategy block.
_QWEN_FREECAD_STRATEGY_V2 = """\

REQUIRED STRATEGY (FreeCAD, structured-action vocabulary):

You build geometry via TYPED actions, NOT raw python_eval. The action
schema enforces required dimensions, so you cannot type a template with
default numbers — you must estimate from the goal image.

ACTIONS available:
  build_box, build_cylinder, build_sphere, build_torus
  cut, fuse, compound
  frame_view, terminate
  python_eval     ← ESCAPE HATCH only (see end)

CHOICE RULE (MANDATORY): if your intended action would be
`python_eval(code)` where `code` only calls makeBox / makeCylinder /
makeSphere / makeTorus / cut / fuse / makeCompound, you MUST emit the
corresponding typed action instead. Typed actions remove syntax errors
and missing-dimension errors.

DIMENSION ESTIMATION: in your `rationale`, write the bbox estimate FIRST
(e.g. "door ≈ 940×160×2120 mm"), then emit the build_* with those exact
numbers.

NAMING: the user prompt passes a GOAL_NAME — use it as the `name` arg.

WORKED EXAMPLES (one per asset family):

  Single box (door panel / plate / brick / slab):
    {"type":"build_box","dims":{"x":940,"y":160,"z":2120},
     "origin":{"x":0,"y":0,"z":0},"name":"door_panel"}

  Single cylinder (pipe / shaft / bolt body):
    {"type":"build_cylinder","radius":30,"height":150,"axis":"z",
     "origin":{"x":0,"y":0,"z":0},"name":"shaft"}

  Single sphere (ball / lamp head):
    {"type":"build_sphere","radius":100,
     "origin":{"x":0,"y":0,"z":100},"name":"head"}

  Single torus (ring / gasket / chain link):
    {"type":"build_torus","major_radius":100,"minor_radius":15,
     "origin":{"x":0,"y":0,"z":0},"name":"ring"}

  Door with handle hole (box minus cylinder):
    1. {"type":"build_box","dims":{"x":900,"y":40,"z":2100},
        "origin":{"x":0,"y":0,"z":0},"name":"panel"}
    2. {"type":"build_cylinder","radius":25,"height":50,"axis":"y",
        "origin":{"x":820,"y":-5,"z":1050},"name":"hole"}
    3. {"type":"cut","from":"panel","by":"hole","name":"door"}

  Bracket with hole (compositional, same pattern as door):
    1. build_box (small dims, e.g. 100×60×10)
    2. build_cylinder (small bore through z)
    3. cut

  Pulley (disk with center bore):
    1. {"type":"build_cylinder","radius":40,"height":10,"name":"disk"}
    2. {"type":"build_cylinder","radius":5,"height":20,
        "origin":{"x":0,"y":0,"z":-5},"name":"bore"}
    3. {"type":"cut","from":"disk","by":"bore","name":"pulley"}

  Bearing (3 concentric cylinders, outer cut by middle, fused with inner):
    1. build_cylinder (radius=outer, height=T, name='outer')
    2. build_cylinder (radius=mid,   height=T, name='mid')
    3. build_cylinder (radius=inner, height=T, name='inner')
    4. {"type":"cut","from":"outer","by":"mid","name":"race"}
    5. {"type":"fuse","shapes":["race","inner"],"name":"bearing"}

  Chain of N links (multi-part, no boolean union):
    1..N. build_torus per link with origin offset along z and alternating rotations
    N+1. {"type":"compound","shapes":["link0","link1",...,"link{N-1}"],"name":"chain"}

  Multi-part assembly (door + 4 trims, table + 4 legs, hinge + plates):
    1..M. one build_* per visible distinct part, with origin set to its position
    M+1. {"type":"compound","shapes":[...],"name":"assembly"}
    (Use compound — NOT fuse — to keep parts topologically distinct.)

  Tray / shower pad / pan (large box minus smaller inset box):
    1. {"type":"build_box","dims":{"x":1000,"y":1000,"z":100},"name":"outer"}
    2. {"type":"build_box","dims":{"x":940,"y":940,"z":60},
        "origin":{"x":30,"y":30,"z":40},"name":"inset"}
    3. {"type":"cut","from":"outer","by":"inset","name":"pad"}

AFTER building, emit:
  {"type":"frame_view"}    — isometric + fit-all
  {"type":"terminate"}     — when CURRENT matches GOAL

ESCAPE HATCH (python_eval): use ONLY for shapes not expressible as
primitives + booleans:
  - Revolutions: Part.makeRevolution(profile, axis, angle)
  - Lofts / sweeps: Part.makeLoft, Part.makeSweep
  - Custom Part::Feature subclasses
If you're tempted to use python_eval for a simple Part.makeBox /
Cylinder / Sphere / Torus / cut / fuse, STOP — emit the typed action.
"""

_QWEN_BLENDER_STRATEGY_V2 = """\

REQUIRED STRATEGY (Blender, structured-action vocabulary):

You build geometry via TYPED actions, NOT raw python_eval. python_eval
remains for modifier stacks and curves; primitives + booleans use the
typed verbs below.

ACTIONS available:
  build_box, build_cylinder, build_sphere, build_torus
  cut, fuse, compound
  frame_all, terminate
  python_eval     ← for modifier stacks, curves, text, metaballs only

CHOICE RULE: if your intended action would be `python_eval(code)` where
`code` only calls primitive_cube_add / primitive_uv_sphere_add /
primitive_cylinder_add / primitive_torus_add / BOOLEAN modifier, emit
the typed action instead.

WORKED EXAMPLES:

  Cube (cube/box):
    {"type":"build_box","dims":{"x":2,"y":2,"z":2},
     "origin":{"x":0,"y":0,"z":0},"name":"Cube"}

  Sphere:
    {"type":"build_sphere","radius":1,"origin":{"x":0,"y":0,"z":0},"name":"Sphere"}

  Cylinder:
    {"type":"build_cylinder","radius":1,"height":2,
     "origin":{"x":0,"y":0,"z":0},"name":"Cylinder"}

  Torus:
    {"type":"build_torus","major_radius":1.5,"minor_radius":0.4,
     "origin":{"x":0,"y":0,"z":0},"name":"Torus"}

  Boolean DIFFERENCE (cube with spherical bite):
    1. {"type":"build_box","dims":{"x":2,"y":2,"z":2},"name":"Cube"}
    2. {"type":"build_sphere","radius":1.3,
        "origin":{"x":1,"y":1,"z":1},"name":"Bite"}
    3. {"type":"cut","from":"Cube","by":"Bite","name":"Result"}

  Boolean UNION (multi-primitive merged):
    1. {"type":"build_box","dims":{"x":2,"y":2,"z":2},"name":"A"}
    2. {"type":"build_box","dims":{"x":1.2,"y":1.2,"z":1.2},
        "origin":{"x":1.5,"y":0,"z":0.5},"name":"B"}
    3. {"type":"fuse","shapes":["A","B"],"name":"Result"}

  Multi-object scene (kitbash robot, table+legs, chain of toruses):
    1..N. one build_* per visible distinct object with appropriate origin
    N+1. {"type":"compound","shapes":[...],"name":"assembly"}

  Stack/array of N identical objects:
    1..N. one build_* per copy with varying origin (axis-aligned)
    N+1. compound

ESCAPE HATCH (python_eval): use ONLY for:
  - Modifier stacks (subdivision, bevel, array, mirror, screw)
  - Text objects (bpy.ops.object.text_add)
  - Metaballs
  - Curves and helixes
  - Geometry-nodes / particle scenes
If your code only uses primitive_*_add or BOOLEAN modifiers, USE THE
TYPED ACTIONS INSTEAD.

AFTER building, emit:
  {"type":"frame_all"}    — fit-all
  {"type":"terminate"}    — when CURRENT matches GOAL
"""

_QWEN_FREECAD_REMINDER_V2 = _QWEN_SCHEMA_REMINDER_COMMON + _QWEN_FREECAD_STRATEGY_V2
_QWEN_BLENDER_REMINDER_V2 = _QWEN_SCHEMA_REMINDER_COMMON + _QWEN_BLENDER_STRATEGY_V2


@dataclass
class VLMResponse:
    action: dict[str, Any]
    rationale: str
    raw_content: str
    reasoning_trace: str | None  # the model's chain-of-thought, if returned
    finish_reason: str | None
    usage: dict[str, Any] | None


class OpenRouterVLMClient:
    def __init__(self, api_key: str, model: str,
                 referer: str = "https://github.com/local/cua-smoketest",
                 title: str = "cua-smoketest agent",
                 timeout: float = 120.0,
                 provider_order: list[str] | None = None,
                 provider_ignore: list[str] | None = None,
                 allow_fallbacks: bool = True,
                 reasoning_effort: str = "high",
                 image_max_dim: int = 1920,
                 app: str = "freecad",
                 structured_actions: bool = False):
        if not api_key:
            raise ValueError("OpenRouter API key is required")
        self.api_key = api_key
        self.model = model
        self.referer = referer
        self.title = title
        self.timeout = timeout
        # Pin / prefer specific upstream providers when set. Useful when a
        # default provider is unhealthy (e.g. Novita timing out on Gemma 4).
        self.provider_order = provider_order
        self.provider_ignore = provider_ignore
        self.allow_fallbacks = allow_fallbacks
        if reasoning_effort not in ("low", "medium", "high"):
            raise ValueError(f"reasoning_effort must be low/medium/high, got {reasoning_effort!r}")
        self.reasoning_effort = reasoning_effort
        # Longest image edge (px) sent to the VLM. PNG screenshots from
        # 1920x1080 Xvfb are downscaled (preserving aspect) if either
        # dimension exceeds this. Smaller = fewer vision tokens =
        # faster + cheaper, at the cost of fine UI detail.
        self.image_max_dim = image_max_dim
        # Model-family flag. Only Qwen3-VL gets the strict-schema reminder
        # and post-parse click coercion. Gemma path is byte-identical to
        # the pre-patch behavior.
        self._is_qwen = "qwen" in model.lower()
        # App-aware Qwen reminder. FreeCAD agents see FC Python-console
        # Strategy B; Blender agents see bpy Strategy B.
        # v2 structured-actions experiment: when enabled, REPLACE the
        # python_eval-templates strategy with a structured-vocabulary
        # strategy (typed build_*/cut/fuse/compound). v1 PREPENDED and was
        # ignored because the templates section below it kept showing the
        # agent python_eval examples to pattern-match on. v2 swaps the
        # whole strategy. python_eval remains as escape hatch.
        self._structured_actions = bool(structured_actions and self._is_qwen)
        if self._structured_actions:
            self._qwen_reminder = _QWEN_BLENDER_REMINDER_V2 if app == "blender" else _QWEN_FREECAD_REMINDER_V2
        else:
            self._qwen_reminder = _QWEN_BLENDER_REMINDER if app == "blender" else _QWEN_FREECAD_REMINDER

    # --- public API --------------------------------------------------------

    def next_action(self, system_prompt: str, goal_png: Path,
                    current_png: Path, step_idx: int,
                    max_history_hint: str | None = None) -> VLMResponse:
        user_text = (
            f"Step {step_idx}. The two attached images are the GOAL_STATE "
            f"(target the cursor should drive FreeCAD toward) and the "
            f"CURRENT_STATE (what the screen looks like right now). "
            f"Output exactly one JSON object: "
            f'{{"action": <action object>, "rationale": "<one or two sentences>"}}. '
            f"If the CURRENT_STATE already matches the GOAL_STATE, return "
            f'{{"action": {{"type": "terminate"}}, "rationale": "goal reached"}}.'
        )
        if max_history_hint:
            user_text += f"\n\nRecent history:\n{max_history_hint}"
        if self._is_qwen:
            user_text += "\n\n" + self._qwen_reminder
            # Inject GOAL_NAME parsed from the goal screenshot filename so the
            # agent can name its object to match (free name_overlap points).
            goal_name = _extract_goal_name(goal_png)
            if goal_name:
                user_text += f"\n\nGOAL_NAME = '{goal_name}'  (use as the third arg to addObject / object name)"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": "GOAL_STATE (target):"},
                self._image_block(goal_png),
                {"type": "text", "text": "CURRENT_STATE (now):"},
                self._image_block(current_png),
                {"type": "text", "text": user_text},
            ]},
        ]

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            # Total completion budget. Reasoning + visible content must fit
            # together; with effort:medium we still want plenty of room for
            # the JSON action object after the model thinks.
            # Generous completion budget so high-effort reasoning has room
            # to think AND emit the JSON action object afterwards.
            "max_tokens": 8192,
            # OpenRouter rejects passing both effort and max_tokens; pick one.
            "reasoning": {"effort": self.reasoning_effort},
            # We want JSON back; many models honor this hint.
            "response_format": {"type": "json_object"},
        }
        provider_block: dict[str, Any] = {}
        if self.provider_order:
            provider_block["order"] = list(self.provider_order)
        if self.provider_ignore:
            provider_block["ignore"] = list(self.provider_ignore)
        if self.provider_order or self.provider_ignore:
            provider_block["allow_fallbacks"] = self.allow_fallbacks
            payload["provider"] = provider_block
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self.referer,
            "X-Title": self.title,
        }

        body = self._post_with_retry(headers, payload)

        # OpenRouter sometimes returns an error envelope at 200 status.
        if "error" in body and "choices" not in body:
            err = body["error"]
            raise RuntimeError(f"OpenRouter error: {err}")
        if "choices" not in body or not body["choices"]:
            raise RuntimeError(
                f"OpenRouter response missing 'choices': {json.dumps(body)[:300]}"
            )
        choice = body["choices"][0]
        msg = choice["message"]
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning")
        finish = choice.get("finish_reason")

        # When `content` is empty but the model still produced reasoning
        # text containing a JSON action (common on length-truncated calls),
        # fall back to searching the reasoning trace.
        parse_source = content
        if not parse_source.strip() and isinstance(reasoning, str):
            parse_source = reasoning
        action, rationale = self._parse_action(
            parse_source, fallback_rationale=reasoning,
        )
        if self._is_qwen:
            action = self._coerce_qwen_action(action)
        return VLMResponse(
            action=action,
            rationale=rationale or (reasoning or "")[:500],
            raw_content=content,
            reasoning_trace=reasoning if isinstance(reasoning, str) else None,
            finish_reason=finish,
            usage=body.get("usage"),
        )

    # --- internals ---------------------------------------------------------

    def _post_with_retry(self, headers: dict, payload: dict,
                         max_attempts: int = 8) -> dict:
        """POST with exponential backoff on 5xx / network errors / 200-body 5xx."""
        last_error: str = ""
        for attempt in range(1, max_attempts + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(_ENDPOINT, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                # Covers TimeoutException, NetworkError, RemoteProtocolError
                # ("peer closed connection..."), DecodingError, etc.
                last_error = f"{type(exc).__name__}: {exc}"
                self._backoff(attempt)
                continue
            # Retry on 5xx and on the transient 4xx codes OpenRouter uses
            # when an upstream provider hiccups (405 "Provider returned
            # error", 408 timeout, 429 rate limit).
            if resp.status_code in (405, 408, 429) or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                self._backoff(attempt)
                continue
            if resp.status_code != 200:
                raise RuntimeError(
                    f"OpenRouter HTTP {resp.status_code}: {resp.text[:500]}"
                )
            try:
                body = resp.json()
            except json.JSONDecodeError:
                # OpenRouter sometimes sends only keep-alive newlines when an
                # upstream provider stalls; the body is empty/whitespace.
                # Treat this as a transient failure and retry.
                last_error = f"200 with non-JSON body ({len(resp.text)} bytes whitespace)"
                self._backoff(attempt)
                continue
            # Some 200 responses still carry an upstream error envelope.
            err = body.get("error") if isinstance(body, dict) else None
            if err and isinstance(err, dict):
                code = err.get("code")
                # Retry on upstream 5xx and on "Provider returned error" (405).
                if isinstance(code, int) and (code in (405, 408, 429) or code >= 500):
                    last_error = f"upstream error {code}: {err.get('message', '')}"
                    self._backoff(attempt)
                    continue
            return body
        raise RuntimeError(f"OpenRouter request failed after {max_attempts} attempts: {last_error}")

    @staticmethod
    def _backoff(attempt: int) -> None:
        delay = min(2 ** attempt, 30)
        time.sleep(delay)

    def _image_block(self, png_path: Path) -> dict:
        # Downscale large screenshots to cut vision-token count (a 1920x1080
        # PNG roughly doubles the prefill cost vs. a 1024x576). If the source
        # is already smaller than image_max_dim, we keep it untouched.
        from PIL import Image  # local import: pillow may not always be installed
        import io
        img = Image.open(png_path)
        if max(img.size) > self.image_max_dim:
            img = img.copy()
            img.thumbnail((self.image_max_dim, self.image_max_dim), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
        else:
            data = png_path.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        }

    @staticmethod
    def _parse_action(content: str, fallback_rationale: str | None) -> tuple[dict, str]:
        """Tolerantly pull a JSON object containing {'action': ...} from the
        model's text output. Models sometimes wrap JSON in ```json fences.
        """
        text = content.strip()
        # Strip code fences.
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        # First attempt: parse the whole thing.
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            # Fallback: find the largest balanced {...} substring.
            obj = OpenRouterVLMClient._extract_first_json_object(text)
        if not isinstance(obj, dict):
            raise ValueError(f"model did not return a JSON object; got: {content[:200]}")
        if "action" not in obj:
            # Treat the whole object as the action if it has a 'type' key.
            if "type" in obj:
                return obj, (fallback_rationale or "")[:500]
            raise ValueError(f"response missing 'action' field: {obj}")
        action = obj["action"]
        rationale = obj.get("rationale") or fallback_rationale or ""
        return action, str(rationale)

    @staticmethod
    def _coerce_qwen_action(action: Any) -> Any:
        """Normalize Qwen3-VL's malformed click variants into the canonical
        {"type":"click","x":int,"y":int} shape the ActionExecutor expects.

        Conservative: only rewrites when the input matches a known wrong
        pattern AND the canonical fields are absent. Never overwrites a
        well-formed action. No-op for non-pointing actions.
        """
        if not isinstance(action, dict):
            return action
        if action.get("type") not in {"click", "move_to", "double_click", "right_click"}:
            return action
        if "x" in action and "y" in action and isinstance(action["x"], (int, float)) \
                and isinstance(action["y"], (int, float)):
            return action  # already canonical
        # Variant 1: {"x":[X,Y]} — packed list under "x" (the dominant Qwen bug).
        x = action.get("x")
        if isinstance(x, list) and len(x) == 2 and all(isinstance(v, (int, float)) for v in x):
            action["x"], action["y"] = int(x[0]), int(x[1])
            return action
        # Variant 2: {"coords":[X,Y]} — sibling field, common in Qwen-VL family.
        coords = action.get("coords")
        if isinstance(coords, list) and len(coords) == 2 \
                and all(isinstance(v, (int, float)) for v in coords):
            action["x"], action["y"] = int(coords[0]), int(coords[1])
            action.pop("coords", None)
            return action
        # Variant 3: {"position":{"x":X,"y":Y}} or {"position":[X,Y]}.
        pos = action.get("position") or action.get("point")
        if isinstance(pos, dict) and isinstance(pos.get("x"), (int, float)) \
                and isinstance(pos.get("y"), (int, float)):
            action["x"], action["y"] = int(pos["x"]), int(pos["y"])
            action.pop("position", None); action.pop("point", None)
            return action
        if isinstance(pos, list) and len(pos) == 2 \
                and all(isinstance(v, (int, float)) for v in pos):
            action["x"], action["y"] = int(pos[0]), int(pos[1])
            action.pop("position", None); action.pop("point", None)
            return action
        return action

    @staticmethod
    def _extract_first_json_object(text: str) -> Any:
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    snippet = text[start:i + 1]
                    try:
                        return json.loads(snippet)
                    except json.JSONDecodeError:
                        continue
        raise ValueError("no balanced JSON object found in response")
