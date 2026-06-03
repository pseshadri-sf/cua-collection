"""Build a self-contained HTML visualization for a parallel-orchestrator batch run.

Point it at a run dir and it writes <run_dir>/index.html — a sortable/filterable
grid of cards, one per job, each showing:
  - GOAL state (goal.png)
  - FINAL state (eval/agent_render.png if present, else the last viewport frame)
  - the trajectory video (lazy-loaded <video>)
  - the reward (match_score) overlaid as a colour-coded badge
  - app, termination reason, steps, duration, and planner+SLM cost

All asset references are RELATIVE to the run dir, so the page is portable: open
<run_dir>/index.html in any browser (or `python -m http.server` in the run dir).

Usage:
    python build_run_viz.py <run_dir> [--eval-jobs-file <jobs.jsonl>] [--open]
"""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path


def _load_eval(run_dir: Path) -> dict[str, float]:
    f = run_dir / "eval_results.json"
    out: dict[str, float] = {}
    if f.exists():
        for x in json.loads(f.read_text()):
            if x.get("status") == "ok" and x.get("match_score") is not None:
                out[x["job"]] = max(out.get(x["job"], -1.0), float(x["match_score"]))
    return out


def _summary(run_dir: Path) -> dict[str, dict]:
    f = run_dir / "summary.json"
    if not f.exists():
        return {}
    return {r["job_id"]: r for r in json.loads(f.read_text()).get("results", [])}


def _costs(job_dir: Path) -> tuple[float, float]:
    """(planner_cost, slm_cost) in USD for one job, 0.0 if unavailable."""
    pc = sc = 0.0
    bp = job_dir / "build_plan.json"
    if bp.exists():
        try:
            pc = float((json.loads(bp.read_text()).get("_planner_usage") or {}).get("cost") or 0.0)
        except Exception:
            pass
    tj = job_dir / "trajectory.json"
    if tj.exists():
        try:
            for s in json.loads(tj.read_text()).get("trajectory", []):
                sc += float((s.get("usage") or {}).get("cost") or 0.0)
        except Exception:
            pass
    return pc, sc


def _final_image(job_dir: Path) -> Path | None:
    ar = job_dir / "eval" / "agent_render.png"
    if ar.exists():
        return ar
    frames = sorted((job_dir / "frames").glob("step_*_before.png"))
    return frames[-1] if frames else None


def _rel(p: Path | None, base: Path) -> str | None:
    return os.path.relpath(p, base) if p and p.exists() else None


def collect(run_dir: Path) -> list[dict]:
    ev = _load_eval(run_dir)
    summ = _summary(run_dir)
    cards = []
    for job_dir in sorted(run_dir.glob("worker_*/jobs/*")):
        if not job_dir.is_dir():
            continue  # skip the orchestrator's per-job <job_id>.json sidecars
        jid = job_dir.name
        r = summ.get(jid, {})
        app = "freecad" if jid.startswith("fc__") else ("blender" if jid.startswith("bl__") else (r.get("app") or "?"))
        pc, sc = _costs(job_dir)
        cards.append({
            "job": jid,
            "app": app,
            "score": ev.get(jid),
            "status": r.get("status"),
            "terminated_by": r.get("terminated_by"),
            "steps": r.get("steps"),
            "duration": r.get("duration_sec"),
            "cost": round(pc + sc, 4),
            "goal": _rel(job_dir / "goal.png", run_dir),
            "final": _rel(_final_image(job_dir), run_dir),
            "video": _rel(job_dir / "trajectory.mp4", run_dir),
        })
    return cards


_TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Run viz — __TITLE__</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font:14px/1.4 system-ui,sans-serif;background:#15171c;color:#e6e8ee}
header{position:sticky;top:0;z-index:10;background:#1b1e25;border-bottom:1px solid #2c313c;padding:10px 16px;display:flex;gap:14px;align-items:center;flex-wrap:wrap}
header h1{font-size:15px;margin:0 12px 0 0;font-weight:700}
header .stat{font-size:12px;color:#aab}
select,input{background:#262b34;color:#e6e8ee;border:1px solid #39414f;border-radius:6px;padding:5px 8px;font-size:13px}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:14px;padding:16px}
.card{background:#1b1e25;border:1px solid #2c313c;border-radius:10px;overflow:hidden;display:flex;flex-direction:column}
.card .top{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 10px;border-bottom:1px solid #2c313c}
.card .name{font-size:12px;font-weight:600;word-break:break-all}
.badge{font-weight:700;font-size:13px;padding:3px 9px;border-radius:20px;white-space:nowrap;color:#0b0d10}
.meta{font-size:11px;color:#9aa3b2;padding:4px 10px;display:flex;gap:10px;flex-wrap:wrap}
.meta .tag{background:#262b34;border-radius:4px;padding:1px 6px}
.imgs{display:grid;grid-template-columns:1fr 1fr;gap:2px;background:#0f1115}
.imgs figure{margin:0;position:relative}
.imgs img{width:100%;height:230px;object-fit:contain;background:#0f1115;display:block}
.imgs figcaption{position:absolute;top:4px;left:4px;font-size:10px;background:#000a;padding:1px 6px;border-radius:4px}
video{width:100%;background:#000;max-height:300px}
.missing{height:230px;display:flex;align-items:center;justify-content:center;color:#5a6372;font-size:12px;background:#0f1115}
</style></head><body>
<header>
  <h1>__TITLE__</h1>
  <span class="stat" id="stats"></span>
  <label>sort <select id="sort">
    <option value="score-asc">score ↑ (worst first)</option>
    <option value="score-desc">score ↓ (best first)</option>
    <option value="name">name</option>
    <option value="cost-desc">cost ↓</option>
    <option value="dur-desc">time ↓</option>
  </select></label>
  <label>app <select id="app"><option value="">all</option><option value="freecad">freecad</option><option value="blender">blender</option></select></label>
  <input id="q" placeholder="filter by name…" size="20">
  <label><input type="checkbox" id="lowonly"> score &lt; 55 only</label>
</header>
<div id="grid"></div>
<script>
const DATA = __DATA__;
const grid = document.getElementById('grid');
function color(s){ if(s==null) return '#6b7280'; return s>=80?'#34d399':s>=55?'#fbbf24':'#f87171'; }
function fig(src,label){ return src ? `<figure><img loading="lazy" src="${src}"><figcaption>${label}</figcaption></figure>` : `<div class="missing">${label}: n/a</div>`; }
function card(d){
  const sc = d.score==null?'—':d.score.toFixed(1);
  const vid = d.video ? `<video preload="none" controls src="${d.video}"></video>` : `<div class="missing">no video</div>`;
  return `<div class="card">
    <div class="top"><span class="name">${d.job}</span><span class="badge" style="background:${color(d.score)}">${sc}</span></div>
    <div class="meta"><span class="tag">${d.app}</span><span class="tag">${d.terminated_by||'?'}</span><span class="tag">${d.steps??'?'} steps</span><span class="tag">${d.duration!=null?d.duration.toFixed(0)+'s':'?'}</span><span class="tag">$${(d.cost||0).toFixed(4)}</span></div>
    <div class="imgs">${fig(d.goal,'GOAL')}${fig(d.final,'FINAL')}</div>
    ${vid}
  </div>`;
}
function render(){
  const q=document.getElementById('q').value.toLowerCase();
  const app=document.getElementById('app').value;
  const low=document.getElementById('lowonly').checked;
  const sort=document.getElementById('sort').value;
  let rows=DATA.filter(d=>(!app||d.app===app)&&(!q||d.job.toLowerCase().includes(q))&&(!low||(d.score!=null&&d.score<55)));
  const sc=d=>d.score==null?-1:d.score;
  rows.sort((a,b)=> sort==='score-asc'?sc(a)-sc(b): sort==='score-desc'?sc(b)-sc(a): sort==='cost-desc'?(b.cost||0)-(a.cost||0): sort==='dur-desc'?(b.duration||0)-(a.duration||0): a.job.localeCompare(b.job));
  grid.innerHTML=rows.map(card).join('');
  const scored=DATA.filter(d=>d.score!=null);
  const mean=scored.length?(scored.reduce((s,d)=>s+d.score,0)/scored.length).toFixed(1):'—';
  const fc=scored.filter(d=>d.app==='freecad'), bl=scored.filter(d=>d.app==='blender');
  const m=a=>a.length?(a.reduce((s,d)=>s+d.score,0)/a.length).toFixed(1):'—';
  document.getElementById('stats').textContent=`${DATA.length} jobs · showing ${rows.length} · mean ${mean} (FC ${m(fc)} / BL ${m(bl)})`;
}
['sort','app','q','lowonly'].forEach(id=>document.getElementById(id).addEventListener('input',render));
render();
</script></body></html>"""


def build(run_dir: Path) -> Path:
    cards = collect(run_dir)
    htmltxt = (_TEMPLATE
               .replace("__TITLE__", html.escape(run_dir.name))
               .replace("__DATA__", json.dumps(cards)))
    out = run_dir / "index.html"
    out.write_text(htmltxt)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_dir", type=Path)
    p.add_argument("--open", action="store_true", help="print a file:// URL to open")
    args = p.parse_args(argv)
    if not args.run_dir.exists():
        print(f"run dir not found: {args.run_dir}")
        return 2
    out = build(args.run_dir)
    n = len(collect(args.run_dir))
    print(f"wrote {out}  ({n} jobs)")
    if args.open:
        print(f"open: file://{out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
