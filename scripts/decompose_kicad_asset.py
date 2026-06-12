"""Decompose a KiCad PCB into functional subcircuit clusters.

Run with the system python3 that has pcbnew (KiCad install):
    KIDEC_ASSET=/path/to/board.kicad_pcb KIDEC_OUT=/path/to/out_dir \
        /usr/bin/python3 decompose_kicad_asset.py

Outputs:
    out_dir/
        manifest.json

A PCB doesn't decompose into separate files like a CAD model — there are no
standalone sub-objects to extract. Instead we identify FUNCTIONAL SUBCIRCUITS by
clustering footprints on three independent signals (then taking their union via
union-find), the same way a board layout engineer naturally groups parts:

  1. NET connectivity  — footprints sharing a net are wired together
  2. SPATIAL proximity — same physical region usually = one function
  3. REFDES prefix     — U1/R10/C5 vs U2/R20/C10 family blocks (weak)

The cluster label is heuristic, picked from the "personality" parts that
dominate it (a cluster with a regulator IC + several caps + a diode is "Power";
one with the main MCU + crystal is "MCU/Core"; one centered on a USB connector
is "USB"; etc).

manifest.json schema:
    {
      "asset":      "/orig/path",
      "app":        "kicad",
      "kind":       "subcircuit-clustering",
      "method":     "net+space+refdes union-find",
      "footprint_count": N,
      "cluster_count":   M,
      "clusters": [
        {"index": 0, "label": "Power", "footprint_count": 8,
         "refs": ["U1","C1","C2","D1","L1","R1","R2"],
         "bbox": {"x":..,"y":..,"w":..,"h":..,"cx":..,"cy":..},
         "footprints": [
           {"ref":"U1","name":"...","at":[x,y],"rot":..,"layer":"..","key":true},
           ...]},
        ...
      ]
    }

This is the input to the planner's PER-CLUSTER PLACEMENT prompt block, replacing
the flat per-footprint list that doesn't fit the regional way humans build PCBs.
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import Counter, defaultdict


# ---- union-find ------------------------------------------------------------

class UF:
    def __init__(self, n: int):
        self.p = list(range(n)); self.r = [0]*n
    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a: int, b: int):
        ra, rb = self.find(a), self.find(b)
        if ra == rb: return
        if self.r[ra] < self.r[rb]: ra, rb = rb, ra
        self.p[rb] = ra
        if self.r[ra] == self.r[rb]: self.r[ra] += 1


# ---- heuristic cluster labels ---------------------------------------------

# A footprint's FPID can hint at its function (e.g. "USB_C" or "LDO").
_LABEL_RULES = [
    # (regex over FPID-or-ref, label, priority)
    (re.compile(r"USB", re.I),                        "USB",         9),
    (re.compile(r"HDMI", re.I),                       "HDMI",        9),
    (re.compile(r"Ethernet|RJ45", re.I),              "Ethernet",    9),
    (re.compile(r"Crystal|Oscillator|XTAL", re.I),    "Clock",       8),
    (re.compile(r"Regulator|LDO|Buck|Boost|DCDC", re.I), "Power",    9),
    (re.compile(r"Header|Connector|PinHeader|JST",re.I),"Connector", 7),
    (re.compile(r"Antenna|RF", re.I),                 "RF",          8),
    (re.compile(r"LED", re.I),                        "LED",         5),
    (re.compile(r"Switch|Button|Tactile",re.I),       "Switch",      6),
    (re.compile(r"Battery|Holder",re.I),              "Power_In",    7),
    (re.compile(r"\bMCU\b|ESP32|RP2040|ATmega|STM32|nRF|SAMD|PIC",re.I),"MCU/Core",10),
    (re.compile(r"QFN|QFP|TQFP|BGA|SOIC|TSSOP", re.I),"IC",          4),
    (re.compile(r"Diode|TVS|Zener",re.I),             "Protection",  3),
    (re.compile(r"Inductor",re.I),                    "Power",       3),
]

# A refdes prefix maps to a coarse role
_REF_PREFIX_ROLE = {
    "U": "IC", "Q": "IC", "K": "Relay",
    "R": "Passive", "C": "Passive", "L": "Inductor",
    "D": "Diode", "LED": "LED",
    "J": "Connector", "P": "Connector", "X": "Connector", "CN": "Connector",
    "SW": "Switch", "BT": "Battery", "Y": "Clock", "FB": "Ferrite",
    "MH": "Mount", "TP": "TestPoint",
}


def _refdes_prefix(ref: str) -> str:
    m = re.match(r"^([A-Za-z]+)", ref or "")
    return m.group(1).upper() if m else ""


def _label_cluster(fps_in_cluster: list[dict]) -> tuple[str, list[str]]:
    """Return (label, key_refs).

    Strategy: a "key" footprint (MCU, USB, regulator, crystal, ...) defines the
    cluster's function — even when surrounded by 30 passives. We score every
    footprint against the rules and pick the label whose key footprint has the
    HIGHEST priority. Ties broken by total weighted score (sum of priorities)."""
    label_score: dict[str, tuple[int, int, list[str]]] = {}  # label -> (max_prio, total_prio, key_refs)
    role_counts = Counter()
    for f in fps_in_cluster:
        text = (f.get("name") or "") + " " + (f.get("ref") or "")
        # collect ALL matching rules — not just the highest. A footprint can
        # be both "USB" and "Connector"; the more specific (higher prio) wins.
        best_for_fp = None
        for rx, label, p in _LABEL_RULES:
            if rx.search(text):
                if not best_for_fp or p > best_for_fp[1]:
                    best_for_fp = (label, p)
        if best_for_fp:
            lab, p = best_for_fp
            mx, total, refs = label_score.get(lab, (0, 0, []))
            mx = max(mx, p); total += p
            refs.append(f["ref"])
            label_score[lab] = (mx, total, refs)
        role_counts[_REF_PREFIX_ROLE.get(_refdes_prefix(f["ref"]), "?")] += 1

    if label_score:
        # pick by highest single-footprint priority, ties by total signal
        best = max(label_score.items(), key=lambda kv: (kv[1][0], kv[1][1]))
        lab, (_, _, refs) = best
        # Bucket: if only a single low-priority rule fired in a big passive
        # cluster, fall back to the dominant role (so 30 caps + 1 minor diode
        # doesn't get labeled "Protection")
        if best[1][0] < 5 and len(fps_in_cluster) >= 6:
            top = role_counts.most_common(1)[0][0]
            if top in ("Passive", "Connector"): return top, refs[:4]
        return lab, refs[:4]
    if role_counts:
        return role_counts.most_common(1)[0][0], []
    return "Unknown", []


# ---- spatial bbox helpers --------------------------------------------------

def _cluster_bbox(fps: list[dict]) -> dict:
    xs = [f["at"][0] for f in fps]; ys = [f["at"][1] for f in fps]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    return {"x": round(x0, 2), "y": round(y0, 2),
            "w": round(x1 - x0, 2), "h": round(y1 - y0, 2),
            "cx": round((x0 + x1)/2, 2), "cy": round((y0 + y1)/2, 2)}


# ---- main ------------------------------------------------------------------

def main() -> int:
    asset = os.environ.get("KIDEC_ASSET")
    out_dir = os.environ.get("KIDEC_OUT")
    if not (asset and out_dir):
        print("ERROR: KIDEC_ASSET and KIDEC_OUT env vars required", file=sys.stderr)
        return 2
    os.makedirs(out_dir, exist_ok=True)

    import pcbnew  # type: ignore[import-not-found]
    board = pcbnew.LoadBoard(asset)
    fps = list(board.GetFootprints())
    n = len(fps)
    if n == 0:
        json.dump({"asset": asset, "app": "kicad", "kind": "subcircuit-clustering",
                   "footprint_count": 0, "cluster_count": 0, "clusters": [],
                   "error": "no footprints"},
                  open(os.path.join(out_dir, "manifest.json"), "w"), indent=2)
        return 0

    # Build per-footprint record
    rec = []
    for i, f in enumerate(fps):
        pos = f.GetPosition()
        x, y = pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y)
        # collect this footprint's nets
        nets = set()
        for pad in f.Pads():
            nc = pad.GetNetCode()
            if nc and nc > 0:
                nets.add(nc)
        rec.append({
            "i": i, "ref": f.GetReference(),
            "name": f.GetFPIDAsString(),
            "at": [round(x, 2), round(y, 2)],
            "rot": round(f.GetOrientationDegrees(), 1),
            "layer": "B.Cu" if f.IsFlipped() else "F.Cu",
            "nets": nets,
        })

    # ===== Anchor-based clustering =====
    # A board layout engineer groups parts AROUND key components (ICs,
    # connectors, large modules). We replicate that: identify "anchor"
    # footprints (any IC by refdes U/Q/IC, large connectors J/P/CN, modules,
    # crystals, batteries), then assign every remaining (passive) footprint to
    # its NEAREST anchor by Euclidean distance. Net-connectivity is used to
    # refine: a passive that shares a non-power net with a non-nearest anchor
    # is re-routed to that anchor instead (handles e.g. the case where R1 sits
    # between two ICs but its pull-up net is owned by IC2).
    ANCHOR_PREFIXES = {"U", "Q", "IC", "J", "P", "CN", "X", "Y", "MOD", "BT",
                       "K", "L"}  # K=relay, L=inductor (often power)
    POWER_NAMES = {"GND", "GNDA", "GNDD", "AGND", "VCC", "VDD", "VBUS", "+5V",
                   "+3V3", "+12V", "VIN", "+VBAT", "VBAT", "EARTH", "NC", "NONE", ""}

    def _is_power_net(nc: int) -> bool:
        try:
            nm = board.FindNet(nc).GetNetname()
        except Exception:
            return True
        stripped = nm.split("/")[-1].upper().lstrip("+")
        return stripped in POWER_NAMES or stripped.startswith("GND") or stripped.startswith("VCC")

    # Compute anchor set
    anchor_idx = []
    for r in rec:
        pref = _refdes_prefix(r["ref"])
        if pref in ANCHOR_PREFIXES:
            anchor_idx.append(r["i"])

    # Fallback: if very few anchors (small board with no ICs), every footprint
    # is its own anchor, then we'll merge small clusters below.
    if len(anchor_idx) < 2:
        anchor_idx = [r["i"] for r in rec]

    # Build NEAREST-ANCHOR assignment for every footprint
    anchor_pts = [(rec[i]["at"][0], rec[i]["at"][1]) for i in anchor_idx]
    assignment = [None] * n
    for r in rec:
        if r["i"] in anchor_idx:
            # an anchor is its own cluster
            assignment[r["i"]] = anchor_idx.index(r["i"])
            continue
        x, y = r["at"]
        best_a = 0; best_d = float("inf")
        for ai, (ax, ay) in enumerate(anchor_pts):
            d = math.hypot(x - ax, y - ay)
            if d < best_d: best_d = d; best_a = ai
        assignment[r["i"]] = best_a

    # Net-based refinement: passives that share a non-power net with an anchor
    # get pulled toward that anchor (overrides Euclidean assignment).
    anchor_set = set(anchor_idx)
    # Build anchor -> nets index (small)
    anchor_nets: dict[int, set[int]] = {}  # anchor_idx_index -> set of netcodes
    for ai, idx in enumerate(anchor_idx):
        anchor_nets[ai] = {nc for nc in rec[idx]["nets"] if not _is_power_net(nc)}
    for r in rec:
        if r["i"] in anchor_set: continue
        sig_nets = {nc for nc in r["nets"] if not _is_power_net(nc)}
        if not sig_nets: continue
        # find which anchors share these nets
        candidates = []
        for ai, nets in anchor_nets.items():
            shared = nets & sig_nets
            if shared:
                ax, ay = anchor_pts[ai]
                d = math.hypot(r["at"][0]-ax, r["at"][1]-ay)
                candidates.append((len(shared), -d, ai))  # more shared = stronger, closer = stronger
        if candidates:
            candidates.sort(reverse=True)
            assignment[r["i"]] = candidates[0][2]

    # Collect clusters
    groups: dict[int, list[dict]] = defaultdict(list)
    for r in rec:
        groups[assignment[r["i"]]].append(r)

    # Small-cluster merge: any singleton anchor cluster (<=2 fp) gets merged
    # to its nearest sibling cluster by centroid. Stops over-fragmentation when
    # multiple ICs sit very close.
    big = [g for g in groups.values() if len(g) >= 3]
    if big:
        def centroid(g):
            xs = [r["at"][0] for r in g]; ys = [r["at"][1] for r in g]
            return sum(xs)/len(xs), sum(ys)/len(ys)
        big_c = [(centroid(g), g) for g in big]
        new_groups = {id(g): g for g in big}
        for g in groups.values():
            if len(g) >= 3: continue
            for r in g:
                rx, ry = r["at"]
                best = min(big_c, key=lambda bc: math.hypot(bc[0][0]-rx, bc[0][1]-ry))
                new_groups[id(best[1])].append(r)
        clusters_raw = list(new_groups.values())
    else:
        clusters_raw = list(groups.values())
    # sort by footprint count desc
    clusters_raw.sort(key=lambda c: -len(c))
    space_thresh = 0.0
    nn_med = 0.0
    # for params logging (kept for back-compat with manifest schema)
    pts = [(r["at"][0], r["at"][1]) for r in rec]

    # Build the manifest
    clusters = []
    for ci, fps_in in enumerate(clusters_raw):
        label, key_refs = _label_cluster(fps_in)
        # Mark key footprints (the ones that determined the label)
        key_set = set(key_refs)
        out_fps = []
        for r in fps_in:
            out_fps.append({
                "ref": r["ref"], "name": r["name"],
                "at": r["at"], "rot": r["rot"], "layer": r["layer"],
                "key": r["ref"] in key_set,
            })
        clusters.append({
            "index": ci,
            "label": label,
            "footprint_count": len(fps_in),
            "refs": [r["ref"] for r in fps_in][:64],  # cap for readability
            "bbox": _cluster_bbox(fps_in),
            "footprints": out_fps,
        })

    manifest = {
        "asset": asset,
        "app": "kicad",
        "kind": "subcircuit-clustering",
        "method": "net+space union-find",
        "params": {"space_thresh_mm": round(space_thresh, 2),
                   "nn_median_mm": round(nn_med, 2)},
        "footprint_count": n,
        "cluster_count": len(clusters),
        "clusters": clusters,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"[ok] {n} footprints -> {len(clusters)} clusters at {space_thresh:.1f}mm -> {out_dir}")
    return 0


if __name__ == "__main__" or "pcbnew" in sys.modules:
    raise SystemExit(main())
