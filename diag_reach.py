"""Which terminals of a net no track can reach (flood fill over the exported maps)."""
import json, sys
import numpy as np
from scipy import ndimage

board, meta_path, codes = sys.argv[1], sys.argv[2], [int(c) for c in sys.argv[3:]]
meta = json.load(open(meta_path))
L, R, C = meta["shape"]; k = len(meta["classes"]); n = L * R * C
raw = open(board, "rb").read()
h = np.frombuffer(raw, np.int32, 6); off = 4 * (6 + k + k * k)
trace = []
for _ in range(k):
    trace.append(np.frombuffer(raw, np.int16, n, off).reshape(L, R, C)); off += 2 * n
via = []
for _ in range(k):
    via.append(np.frombuffer(raw, np.int16, R * C, off).reshape(R, C)); off += 2 * R * C
core = []
for _ in range(k):
    core.append(np.frombuffer(raw, np.int16, n, off).reshape(L, R, C)); off += 2 * n
off += 2 * n  # pad_net
terminals = {}
for _ in range(h[4]):
    code, cls, count = np.frombuffer(raw, np.int32, 3, off); off += 12
    cells = []
    for _ in range(count):
        size = int(np.frombuffer(raw, np.int32, 1, off)[0]); off += 4
        cells.append(np.frombuffer(raw, np.uint32, size, off)); off += 4 * size
    terminals[int(code)] = (int(cls), cells)
x0, y0, pitch = meta["x0"], meta["y0"], meta["pitch"]
for code in codes:
    cls, cells = terminals[code]
    owner = np.where((trace[cls] == -2) & (core[cls] >= 0), core[cls], trace[cls])
    open_ = (owner == -1) | (owner == code)
    viaok = (via[cls] == -1) | (via[cls] == code)
    both = open_[0] & open_[1] & viaok
    lab0, _ = ndimage.label(open_[0], np.ones((3, 3))); lab1, _ = ndimage.label(open_[1], np.ones((3, 3)))
    # join layer components through via-able cells
    import collections
    parent = {}
    def f(a):
        while parent.get(a, a) != a: a = parent[a]
        return a
    for a, b in set(zip(lab0[both].tolist(), lab1[both].tolist())):
        if a and b: parent[f(("0", a))] = f(("1", b))
    comps = []
    for i, t in enumerate(cells):
        ids = set()
        for cell in t[:2000]:
            l, r, c = cell // (R * C), (cell % (R * C)) // C, cell % C
            lab = (lab0 if l == 0 else lab1)[r, c]
            if lab: ids.add(f((str(l), int(lab))))
        comps.append(ids)
    groups = collections.Counter(frozenset(x) for x in comps)
    print(meta["net_names"][str(code)], "terminals", len(cells), "groups", len(groups), "terminals with no open cell", sum(not c for c in comps))
    main = max(groups, key=groups.get)
    for i, ids in enumerate(comps):
        if not (ids & main):
            p = meta["nets"][str(code)]["pads"][i]
            print("   cut off:", round(p["x"], 2), round(p["y"], 2), "fixed" if p.get("fixed") else "pad", "open cells" if ids else "NO open cell")
