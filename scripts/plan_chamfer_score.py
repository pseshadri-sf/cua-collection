"""CPU Chamfer shape score for a candidate build (run under blender -b -P).

Used by FrontierPlanner.plan_best_of to pick the better of N candidate builds.
Reconstructs the candidate by exec-ing its code chunk(s), loads the goal asset,
samples surface points on each, and maps the symmetric normalised Chamfer
distance to a 0-100 score (poly-count-agnostic; cf. 3DCodeBench).

env: CH_GOAL=<goal .blend/.obj/...>  CH_CHUNKS=<json list of code strings>  CH_OUT=<json out>
"""
import bpy, json, os, math, random, bisect
from mathutils import Vector
from mathutils.kdtree import KDTree

random.seed(0)
N = 3000


def reset():
    bpy.ops.wm.read_homefile(use_empty=True)


def sample_surface(n):
    tris = []
    deps = bpy.context.evaluated_depsgraph_get()
    for o in bpy.data.objects:
        if o.type != 'MESH':
            continue
        oe = o.evaluated_get(deps)
        me = oe.to_mesh()
        me.calc_loop_triangles()
        mw = o.matrix_world
        for t in me.loop_triangles:
            a, b, c = (mw @ me.vertices[i].co for i in t.vertices)
            area = (b - a).cross(c - a).length * 0.5
            if area > 0:
                tris.append((area, a, b, c))
        oe.to_mesh_clear()
    if not tris:
        return []
    total = sum(t[0] for t in tris)
    cum = []
    s = 0.0
    for t in tris:
        s += t[0] / total
        cum.append(s)
    pts = []
    for _ in range(n):
        idx = min(bisect.bisect_left(cum, random.random()), len(tris) - 1)
        _, a, b, c = tris[idx]
        u, v = random.random(), random.random()
        if u + v > 1:
            u, v = 1 - u, 1 - v
        pts.append(a + u * (b - a) + v * (c - a))
    return pts


def diag():
    mn = [1e18] * 3; mx = [-1e18] * 3
    for o in bpy.data.objects:
        if o.type != 'MESH':
            continue
        for v in o.bound_box:
            w = o.matrix_world @ Vector(v)
            for i in range(3):
                mn[i] = min(mn[i], w[i]); mx[i] = max(mx[i], w[i])
    if mn[0] > mx[0]:
        return 1.0
    return math.sqrt(sum((mx[i] - mn[i]) ** 2 for i in range(3))) or 1.0


def directed(src, tree):
    return sum(tree.find(p)[2] for p in src) / max(1, len(src))


def main():
    chunks = json.load(open(os.environ['CH_CHUNKS']))
    res = {'score': 0.0}
    try:
        reset()
        ns = {'bpy': bpy}
        for code in chunks:
            try:
                exec(code, ns)
            except Exception:
                pass
        ap = sample_surface(N)
        reset()
        bpy.ops.wm.open_mainfile(filepath=os.environ['CH_GOAL'])
        gp, gd = sample_surface(N), diag()
        if ap and gp:
            ta = KDTree(len(ap)); [ta.insert(p, i) for i, p in enumerate(ap)]; ta.balance()
            tg = KDTree(len(gp)); [tg.insert(p, i) for i, p in enumerate(gp)]; tg.balance()
            ch = 0.5 * (directed(ap, tg) + directed(gp, ta)) / gd
            res = {'score': round(100.0 * math.exp(-6.0 * ch), 1), 'chamfer_norm': round(ch, 4),
                   'agent_pts': len(ap)}
        else:
            res = {'score': 0.0, 'agent_pts': len(ap), 'goal_pts': len(gp)}
    except Exception as e:
        res = {'score': 0.0, 'error': '%s: %s' % (type(e).__name__, e)}
    json.dump(res, open(os.environ['CH_OUT'], 'w'))
    print('PLAN_CHAMFER', json.dumps(res))


main()
