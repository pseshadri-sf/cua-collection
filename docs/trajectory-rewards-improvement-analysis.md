# Research analysis: improving trajectory rewards

## 1. Where we are (sweep120-v2, n=120, Qwen3-VL-30B baseline)

Current means: FC **32.6**, BL **54.2**, overall **42.2** / 100.

Per-component achievement vs ceiling (the headroom map):

| App | Component | Weight | % of ceiling earned | Headroom |
|---|---|---:|---:|---:|
| FC | vol_ratio | 25 | **15.3%** | **21 pts/asset** |
| FC | vert_ratio | 10 | 18.0% | 8.2 pts |
| FC | face_ratio | 15 | 21.0% | 11.9 pts |
| FC | bbox_score | 25 | 34.0% | 16.5 pts |
| FC | name_overlap | 10 | **0.0%** | **10 pts (free)** |
| FC | obj_present | 15 | 98.8% | ~ |
| BL | vol_ratio | 25 | 43.3% | 14.2 pts |
| BL | bbox_score | 25 | 55.5% | 11.1 pts |
| BL | name_overlap | 10 | 45.1% | 5.5 pts |
| BL | face_ratio | 15 | 47.6% | 7.9 pts |
| BL | vert_ratio | 10 | 49.6% | 5.0 pts |
| BL | obj_present | 15 | 92.5% | 1.1 pts |

Theoretical FC ceiling if every component went to 100%: 32.6 → 100. Practical
ceiling (recoverable) is more like ~70-75 — diminishing returns on vol_ratio
above 0.85 since exact volume match requires exact dimensions from a low-res
screenshot.

## 2. Six improvement axes — feasibility / cost / expected impact

| # | Axis | Effort | Cost/run delta | Expected FC lift | Recommended |
|---|---|---|---|---|---|
| **A** | **Tool-calling for structured actions** | medium (~1 day) | neutral | **+8-15** | **YES, top priority** |
| **B** | Few-shot in-context examples | small (~2 hr) | +20% prompt tokens | +3-8 | **YES** |
| **C** | Best-of-N sampling + eval-based selection | medium (~half day) | N× cost | +5-10 | YES (selective) |
| **D** | Model swap to Qwen3-VL-Thinking variant | trivial (model id) | 3-4× completion cost | +4-10 | YES (worth testing) |
| **E** | Scratchpad / explicit dimension-reasoning step | small | +30% completion | +3-5 | YES |
| **F** | RL fine-tuning with eval as reward | very large (weeks) | one-time training $$$ | +15-30 | NO (not yet) |
| **G** | Smarter decomposition (heuristic-gated) | small | 1-3× cost on triggered | +5-10 on triggered | YES |
| **H** | Self-hosted model with constrained decoding | medium-large | infra cost | +10-20 | DEFER (depends on A) |

## 3. Why tool-calling is the highest-leverage single change

Our two structured-action experiments (v1 additive, v2 replacement) both failed
because Qwen3-VL ignores JSON-in-text action schemas — it pattern-matches on
Python code from prompt examples. Tool calling is a completely different
decoding path:

- The model's `tools` parameter declares each action (`build_box`, `cut`, etc.)
  as a function signature
- The model's API output includes a structured `tool_calls` array — not
  text-with-JSON-inside
- The signature is enforced by the model's own decoder; missing/wrong fields
  are syntactically impossible
- Qwen3-VL-30B advertises tool-calling support; OpenRouter exposes it via the
  standard `tools=[...]` parameter

This is the same mechanism that successfully gets coding agents to consistently
emit valid `read_file(path: str)` calls. The vol_ratio and bbox failures we see
come from the agent's Python code typing `makeBox(100,60,10)` (template
defaults). With tool calling, `build_box(dims: {x,y,z})` becomes a
required-typed signature — the model cannot emit it without committing to
specific numbers.

Estimated impact: 0% adoption → 50-80% adoption based on tool-calling adoption
rates seen in other domains (coding agents, function calling benchmarks).
Combined with the structured-action infra we already have, that translates to
**+8-15 FC pts** — closing roughly half the vol_ratio + bbox gap.

Cost: ~1 day to wire `tools=[...]` into `vlm_client.py`, define schemas for the
7 typed actions, handle tool_calls in `_parse_action`.

## 4. Other improvements ranked by leverage

### B. Few-shot in-context examples (high ROI, low effort)

Currently the prompt has abstract templates. Add 3-5 worked examples like:

> Goal image: [tray/pad screenshot] →
> `python_eval(doc=App.newDocument();...makeBox(1000,1000,80)...makeBox(940,940,40)...cut...)`
> Score: 0.74 (good)

Models learn behavioural patterns from examples ~5× more reliably than from
rule text. We have hundreds of trajectories — pick top-K by score, distill into
3-5 generic exemplars, prepend.

Expected lift: +3-8 FC pts. Cost: 20% more prompt tokens (~$0.15 per 100 jobs).

### C. Best-of-N sampling + select-by-eval

For each asset, sample N=3 trajectories in parallel, run the geometric
evaluator on each, return the highest-scoring one. The evaluator runs in 1-2s —
fast enough to gate.

Expected lift: variance-based. From our 3-trial hinge data, max-of-3 vs mean is
typically +5-15 pts (because run-to-run noise is ±8-15 on the same goal).
Picking the best of 3 gives the agent more chances to succeed.

Cost: 3× per-asset. Critical: only apply selectively to assets where the
baseline score is in the "high variance" band (30-60). Don't waste 3× cost on
perfect-score Blender cubes.

### D. Model swap: try Qwen3-VL-Thinking

Untested on this pipeline. The "thinking" variants have explicit reasoning
before output — perfect fit for "estimate dimensions, then build". Pricing on
`qwen3-vl-30b-a3b-thinking`: $0.130 + $1.560 (~3× completion cost). Same prompt
tokens though.

| Variant | Prompt $/M | Compl $/M | Per-call cost (4200p+80c) | Test priority |
|---|---:|---:|---:|---|
| qwen3-vl-30b-a3b-instruct (current) | 0.130 | 0.520 | $0.00059 | baseline |
| qwen3-vl-30b-a3b-**thinking** | 0.130 | 1.560 | $0.00067 | HIGH — same prompt cost, +14% per call for explicit reasoning |
| qwen3-vl-32b-instruct | 0.104 | 0.416 | $0.00047 | MEDIUM — 20% cheaper, unknown quality |
| qwen3-vl-8b-instruct | 0.080 | 0.500 | $0.00038 | LOW — likely worse |
| qwen3-vl-235b-a22b-thinking | 0.260 | 2.600 | $0.00130 | MEDIUM — heavy thinking model |

Expected lift from `30b-a3b-thinking`: +4-10 FC pts based on Qwen's reported
gains on reasoning benchmarks. Marginal cost: ~$0.05 per 100-asset sweep.

### E. Explicit scratchpad / dimension-reasoning prefix

Currently the agent jumps straight to the action. Add: "Before emitting, write
`BBOX_ESTIMATE: WxDxH = ...` in your rationale." We tried this in sweep20_fixes
but only as a sentence buried in the prompt. A required field with explicit
format would force more structured reasoning.

Expected lift: +3-5 FC vol_ratio pts.

### F. RL fine-tuning with eval-as-reward (deferred)

Strong theoretical lift but requires:
- ~$5-50K training runs
- GPU access (8× H100 typical)
- Dataset of ~5K curated trajectories
- 2-4 weeks of work

Not the right next step. Revisit if we have stable, demonstrably plateaued
performance from cheaper interventions first.

### G. Selective decomposition

From our validation: decomposition helps only when whole-asset baseline ≤ 30
AND a dominant-volume part is a simple primitive. Heuristic at job-build time:

```
if whole_baseline_estimate < 30 and max_part_vol_frac > 0.5 and dominant_part_is_primitive():
    expand_into_sub_jobs()
```

Expected impact: +5-10 pts on the ~15% of assets that hit the trigger.

### H. Self-hosted constrained decoding (defer)

vLLM + grammar-constrained generation (e.g., outlines library) forces output to
match a JSON schema bytewise. Strongest possible adoption guarantee but
requires self-hosting. Defer until A (tool-calling) is verified — tool-calling
gives 80% of the benefit at 0% of the infra.

## 5. Open-source vision-language model survey

### On OpenRouter (zero-friction A/B)

| Model | Pricing (p/c) | Vision? | Notes |
|---|---|---|---|
| **qwen/qwen3-vl-30b-a3b-thinking** | 0.130 / 1.560 | ✓ | First to try — thinking variant of current best |
| qwen/qwen3-vl-32b-instruct | 0.104 / 0.416 | ✓ | Untested, cheaper |
| qwen/qwen3-vl-235b-a22b-thinking | 0.260 / 2.600 | ✓ | Big + thinking — expensive |
| google/gemma-4-31b-it | 0.120 / 0.370 | ✓ | Original baseline; ignored macros without Qwen patch |
| mistralai/pixtral-large-2411 | 2.000 / 6.000 | ✓ | Premium; ~16× current cost |
| qwen/qwen2.5-vl-72b-instruct | 0.250 / 0.750 | ✓ | Tested — worse than 30B, drop |

### Not on OpenRouter (need self-hosting — defer until needed)

| Model | Why interesting |
|---|---|
| InternVL3-78B | Strong CAD-leaning benchmark scores; better OCR which helps reading dimensions |
| Molmo-72B (Allen AI) | Trained with pointing/bounding-box-prediction objectives — relevant for goal-asset bbox matching |
| LLaVA-OneVision-72B | Multi-image native; could compare goal + current side-by-side without our merge step |
| DeepSeek-VL2-27B | Mixture-of-experts; lower active params per call |
| MiniCPM-V-2.6 (8B) | Strong few-shot adaptation on small footprint |
| Phi-3.5-vision (4B) | Microsoft's small multimodal; cheap experimental baseline |

## 6. Recommended experiment sequence (in order)

### Wave 1 — quick wins (this week)

1. Tool calling experiment (1 day) — implement `tools=[...]` for the 7 typed
   actions, run 20-asset benchmark. Expected: 0→50%+ adoption, +5-10 FC, +3-5 BL.
2. Qwen3-VL-30B-thinking swap (30 min) — just change model id, run 20-asset
   benchmark. Expected: +4-10 FC.
3. Few-shot examples in prompt (2 hr) — pick top-3 trajectories from
   sweep120-v2, distill into 3 prompt examples, run 20-asset benchmark.
   Expected: +3-8 FC.

These three are independent and stackable. Total ETA: 2 days. Combined expected
lift: FC 32 → 50-65, BL 54 → 65-75.

### Wave 2 — selective improvements (next sprint)

4. Best-of-3 sampling with eval-gating on hard assets. 3× cost only on assets
   in the 30-60 score band; saves money on the 0-30 (hopeless) and 60+ (already
   good).
5. Heuristic-gated decomposition — re-validate the door_with_trims +31 result,
   ship the trigger logic.
6. Scratchpad dimension-reasoning prefix — incremental on top of (3).

### Wave 3 — bigger investments (only if Wave 1+2 plateau)

7. Self-hosted model with vLLM + outlines — InternVL3-78B or Molmo-72B with
   constrained decoding.
8. RL fine-tuning on accumulated trajectory data, with the geometric evaluator
   as the reward signal.

## 7. What I'd ship first if forced to pick ONE

**Tool-calling implementation (#1).** It directly attacks the failure mode we've
now demonstrated across two structured-action experiments — that Qwen ignores
prompt instructions for novel action schemas. Tool calling bypasses the issue
entirely by changing the decoding path, has zero cost overhead (you replace
`python_eval(code)` with one tool call of similar token count), and the
executor handlers we already built (`build_*`, `cut`, `fuse`, `compound`) are
ready to receive structured calls.
