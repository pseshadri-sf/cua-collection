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
action was identical to the action you are about to emit AND the
CURRENT_STATE did not change visibly, pick a DIFFERENT action — do
not re-click the same coordinate. The dropdown almost certainly
auto-closed; reach for a compound macro or change strategy.

FORBIDDEN: File>Open, File>Recent, drag-and-drop. The goal is to
CONSTRUCT the geometry — never load it.

Each turn emits exactly ONE action wrapped as:
  {"action": <action object>, "rationale": "<one or two sentences>"}
"""

_QWEN_FREECAD_STRATEGY = """\

REQUIRED STRATEGY (FreeCAD): Reconstruct GOAL_STATE by typing
FreeCAD Python into the Python console:

  1. {"type":"menu_navigate","path":["View","Panels","Python console"]}
        — docks the console at the bottom (y≈930-1010).
  2. {"type":"click","x":700,"y":990}
        — gives keyboard focus to the console's input line.
  3. {"type":"type","text":"<one-line Python that builds the goal shape>"}
        — see template per shape below. Single-line only; no \\n.
        REMEMBER: estimate dimensions from the goal image.
  4. {"type":"key","key":"enter"}        — executes the code.
  5. {"type":"focus_viewport"}           — transfers focus to viewport.
  6. {"type":"key","key":"0"}            — isometric camera.
  7. {"type":"key","key":"v"} then {"type":"key","key":"f"} — fit all.
  8. {"type":"terminate"}                — when CURRENT matches GOAL.

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
"""

_QWEN_FREECAD_REMINDER = _QWEN_SCHEMA_REMINDER_COMMON + _QWEN_FREECAD_STRATEGY
_QWEN_BLENDER_REMINDER = _QWEN_SCHEMA_REMINDER_COMMON + _QWEN_BLENDER_STRATEGY

# Backward-compat alias for any external import — defaults to FreeCAD.
_QWEN_SCHEMA_REMINDER = _QWEN_FREECAD_REMINDER


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
                 app: str = "freecad"):
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
        # Strategy B; Blender agents see bpy Strategy B. Anything else
        # falls back to FC (the more battle-tested template).
        if app == "blender":
            self._qwen_reminder = _QWEN_BLENDER_REMINDER
        else:
            self._qwen_reminder = _QWEN_FREECAD_REMINDER

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
