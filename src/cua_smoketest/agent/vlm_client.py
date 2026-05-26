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
                 allow_fallbacks: bool = True):
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
            "max_tokens": 4096,
            # Ask OpenRouter to surface the model's reasoning trace.
            # OpenRouter rejects passing both effort and max_tokens; pick one.
            # "low" leaves more completion budget for the JSON action object.
            "reasoning": {"effort": "low"},
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
                         max_attempts: int = 4) -> dict:
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
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"OpenRouter returned non-JSON body (200): {resp.text[:300]}"
                ) from exc
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
        delay = min(2 ** attempt, 16)
        time.sleep(delay)

    @staticmethod
    def _image_block(png_path: Path) -> dict:
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
