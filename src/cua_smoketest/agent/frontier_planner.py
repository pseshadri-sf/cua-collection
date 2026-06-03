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
import re
import time
from pathlib import Path
from typing import Any

import httpx

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
You are an expert CAD/3D reconstruction PLANNER for the Blender Python API (bpy).
You are given a GOAL image (rendered views of a target mesh/scene) plus
pre-computed GOAL_METADATA. Produce a complete build PLAN that a less-capable
executor agent will follow to recreate the asset in Blender.

CONVENTIONS (follow exactly):
- Coordinates/sizes are in Blender units; TRUST GOAL_METADATA bbox/part numbers
  over your own estimates. Note any disagreement in `brief`.
- ALWAYS clear the scene first:
  bpy.ops.object.select_all(action='SELECT'); bpy.ops.object.delete()
- Parameters-first: put key dimensions/counts in `parameters`.
- One distinct visible object = one mesh. PLACE EACH at its own `location=(x,y,z)`
  — never leave everything at the origin (they would overlap into one blob).
- Use bpy.ops.mesh.primitive_*_add: cube_add(size=, location=), uv_sphere_add(
  radius=, location=), cylinder_add(radius=, depth=, location=), cone_add,
  torus_add(major_radius=, minor_radius=, location=), monkey_add. For non-uniform
  boxes set the active object's scale after a cube_add. For arrays, a one-line
  list comprehension over locations is fine.
- For repeated/array layouts use a single-line comprehension:
  [bpy.ops.mesh.primitive_cube_add(size=s, location=(x,y,z)) for x in [...] for y in [...]]
- Name the top-level object with GOAL_NAME when provided.

OUTPUT: a single JSON object, no prose outside it, matching this schema:
{
  "brief": "<2-4 sentence brief: what it is, overall size, object breakdown,
            origin/orientation, key assumptions>",
  "shape_class": "single_solid" | "assembly" | "array" | "organic",
  "parameters": { "<name>": <number>, ... },
  "conventions": { "units": "blender", "origin": "<e.g. world-center>", "up": "+Z" },
  "steps": [
    { "i": 1, "name": "<obj>", "op": "primitive_cube_add"|"primitive_uv_sphere_add"|
        "primitive_cylinder_add"|"primitive_cone_add"|"primitive_torus_add"|
        "primitive_monkey_add"|"array"|"modifier",
      "dims": [<numbers>], "origin": [x,y,z], "why": "<short>" },
    ...
  ],
  "full_code": "<ONE-LINE, semicolon-joined, console-ready bpy that clears the
      scene then reconstructs the ENTIRE asset: import bpy; bpy.ops.object.
      select_all(action='SELECT'); bpy.ops.object.delete(); <add+place every
      object> — NO newlines, NO multi-line def/for blocks (list comprehensions OK)>",
  "validation_targets": { "object_count": <int>, "bbox": [x,y,z] },
  "fallback": "<simplest acceptable single-primitive approximation + est. score>"
}

The `full_code` MUST be valid one-line bpy that runs in Blender's console as-is.
Double-check every object has its own location.
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
    return "\n".join(lines)


class FrontierPlanner:
    def __init__(self, api_key: str, model: str = DEFAULT_PLANNER_MODEL,
                 app: str = "freecad",
                 image_max_dim: int = 1024, timeout: float = 240.0,
                 reasoning_effort: str = "low", max_tokens: int = 24000,
                 referer: str = "https://cua-smoketest.local",
                 title: str = "cua-smoketest-planner"):
        if not api_key:
            raise ValueError("FrontierPlanner needs an OpenRouter api_key")
        self.api_key = api_key
        self.model = model
        self.app = app
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

    def plan(self, goal_png: Path, goal_name: str | None = None) -> dict[str, Any] | None:
        """Produce a BUILD_PLAN for the goal. Returns the validated plan dict, or
        None if planning failed (caller falls back to plain SLM execution)."""
        meta = load_goal_metadata(goal_png) or {}
        user_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": "GOAL views:"},
            self._image_block(goal_png),
            {"type": "text", "text": _metadata_text(meta)},
        ]
        if goal_name:
            user_blocks.append({"type": "text",
                                "text": f"GOAL_NAME = '{goal_name}' (name the top object this)"})
        user_blocks.append({"type": "text",
                            "text": "Output the BUILD_PLAN JSON now."})
        system = _PLANNER_SYSTEM_BL if self.app == "blender" else _PLANNER_SYSTEM
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
        # usage/cost (for the cost ablation) + the reasoning trace (for audit /
        # understanding WHY the planner decomposed the asset the way it did).
        plan["_planner_model"] = self.model
        plan["_planner_usage"] = body.get("usage")
        plan["_planner_reasoning"] = reasoning
        return plan

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
        # last-ditch: first {...} span
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise


def _validate_plan(plan: Any) -> bool:
    if not isinstance(plan, dict):
        return False
    fc = plan.get("full_code")
    if not isinstance(fc, str) or len(fc) < 20 or not ("doc" in fc or "bpy" in fc):
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
        lines += [
            "",
            "DO THIS: emit the python_eval below ONCE to build the whole asset. "
            "It is idempotent (reuses+clears one document), so it already creates "
            "the full model in a single shot. After it runs, your NEXT action MUST "
            'be {"type":"terminate"} — do NOT re-emit it. Re-running the same code '
            "wastes steps and will trigger loop-kill. Only emit DIFFERENT code if "
            "AGENT_STATE shows the build genuinely failed or is wrong.",
            f"  {p.get('full_code','')}",
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
