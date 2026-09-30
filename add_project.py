#!/usr/bin/env python3
"""
Field Walk: add a project to the site with one command.

  python add_project.py --id duplex --name "Duplex sample" \
      arch=Duplex_Arch.ifc hvac=Duplex_Mech.ifc plumb=Duplex_Plumbing.ifc

Disciplines: arch, struct, hvac (or mech), plumb, elec, fire, site. Any subset works.
Writes projects/<id>/ (manifest.json, levels/*.json, thumb.svg) next to this script
and adds the project to projects/index.json, which the launch page reads.
Upload the changed projects/ folder to your host. The IFC files are only read.

Requires: pip install ifcopenshell fast_simplification python-fcl numpy
"""
import argparse, base64, collections, datetime, json, os, re, shutil, subprocess, sys, tempfile, time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ALIASES = {'arch': 'arch', 'architecture': 'arch', 'architectural': 'arch', 'struct': 'struct', 'structure': 'struct',
           'structural': 'struct', 'hvac': 'hvac', 'mech': 'hvac', 'mechanical': 'hvac', 'plumb': 'plumb', 'plumbing': 'plumb',
           'elec': 'elec', 'electrical': 'elec', 'fire': 'fire', 'fireprotection': 'fire', 'site': 'site', 'civil': 'site',
           'mep': 'mep', 'services': 'mep'}
ORDER = ['struct', 'arch', 'hvac', 'plumb', 'elec', 'fire', 'site']
MEP = ['hvac', 'plumb', 'elec', 'fire']
SKIP = {'IfcOpeningElement', 'IfcSpace', 'IfcDistributionPort', 'IfcSite', 'IfcBuilding', 'IfcBuildingStorey', 'IfcAnnotation',
        'IfcGrid', 'IfcFastener', 'IfcMechanicalFastener', 'IfcElementAssembly', 'IfcReinforcingBar', 'IfcVirtualElement'}
STRUCTURAL = {'IfcBeam', 'IfcColumn', 'IfcSlab', 'IfcFooting', 'IfcMember', 'IfcWallStandardCase', 'IfcWall', 'IfcPile', 'IfcRoof'}
WALLS = {'IfcWallStandardCase', 'IfcWall'}
PENETRATION = {'IfcSlab', 'IfcWall', 'IfcWallStandardCase', 'IfcRoof'}
MAX_TRIS = 600
CAP = {'hvac': 250, 'potable': 200, 'sewer': 200, 'gas': 200, 'reclaimed': 200, 'fire': 200, 'plumbother': 200, 'elec': 160,
       'telecom': 160, 'structure': 160, 'wall': 400, 'opening': 140, 'curtain': 120, 'ceiling': 300, 'furniture': 120,
       'archmisc': 140, 'site': 3000}
SHRINK = 0.01          # overlaps under ~10 mm count as touching, not clashing
LEVEL_GAP = 2.0        # storeys closer than this (m) are grouped into one level
CAT_COLOR = {'structure': '#a39e96', 'wall': '#b9b5ad', 'hvac': '#1fc2b4', 'potable': '#1f6fe0', 'sewer': '#2e9e44', 'gas': '#f2c52e',
             'reclaimed': '#8a4fd6', 'fire': '#d62a1e', 'elec': '#e0321f', 'telecom': '#f5cf2a', 'plumbother': '#7d8f9c'}


# ------------------------------------------------------------------ utility colour code (same rules the viewer legend uses)
def from_system(name):
    n = (name or '').lower()
    if re.search(r'domestic|potable|cold water|hot water|dcw|dhw', n): return 'potable'
    if re.search(r'sanitary|drain|storm|waste|vent|sewer', n): return 'sewer'
    if re.search(r'reclaim|grey ?water|gray ?water|non-?potable', n): return 'reclaimed'
    if re.search(r'fire|sprinkler|standpipe', n): return 'fire'
    if re.search(r'gas|fuel|oil|petrol', n): return 'gas'
    if re.search(r'air|mechanical|hydronic|chilled|heating|condenser|refrig|exhaust|supply|return', n): return 'hvac'
    return None

def by_type(disc, t, name):
    s = f'{t or ""} {name or ""}'.lower()
    if disc == 'hvac': return 'hvac'
    if disc == 'fire': return 'fire'
    if disc == 'elec':
        if re.search(r'fire alarm|smoke|strobe|horn|pull station|heat detector|annunciator', s): return 'fire'
        if re.search(r'\bdata\b|telephone|\btele|communication|comm |catv|\btv\b|network|intercom|security|camera|card reader|access control|wifi|wireless', s): return 'telecom'
        return 'elec'
    if re.search(r'dwv|cast iron|sanitary|drain|vent|trap|cleanout', s): return 'sewer'
    if re.search(r'gas', s): return 'gas'
    if re.search(r'sprinkler|fire', s): return 'fire'
    if re.search(r'copper|pex|water|valve|meter|sink|shower|washer|closet|lavatory|faucet|toilet|urinal', s): return 'potable'
    return 'plumbother'

def arch_category(cls):
    if cls in WALLS: return 'wall'
    if cls in ('IfcDoor', 'IfcWindow'): return 'opening'
    if cls in ('IfcCurtainWall', 'IfcMember', 'IfcPlate'): return 'curtain'
    if cls == 'IfcCovering': return 'ceiling'
    if cls == 'IfcFurnishingElement': return 'furniture'
    if cls in ('IfcBeam', 'IfcColumn', 'IfcSlab', 'IfcFooting', 'IfcRoof'): return 'structure'
    return 'archmisc'


# ------------------------------------------------------------------ stage 1: one discipline, run in its own process to keep memory down
def extract(disc, path, work, arch_structural):
    import ifcopenshell, ifcopenshell.geom
    import ifcopenshell.util.element as uel, ifcopenshell.util.placement as upl, ifcopenshell.util.unit as uun
    import fast_simplification
    t0 = time.time()
    model = ifcopenshell.open(path)
    origin_file = os.path.join(work, 'origin.json')
    origin = np.array(json.load(open(origin_file))) if os.path.exists(origin_file) else None
    settings = ifcopenshell.geom.settings(); settings.set('use-world-coords', True)
    skip = [e for c in SKIP for e in model.by_type(c)] if model.schema else []
    it = ifcopenshell.geom.iterator(settings, model, 1, exclude=skip) if skip else ifcopenshell.geom.iterator(settings, model, 1)
    clash_set = None if disc in MEP + ['mep'] else (STRUCTURAL if disc == 'struct' else (WALLS | STRUCTURAL if arch_structural else WALLS) if disc == 'arch' else set())
    systems = {}
    if disc in MEP + ['mep']:
        for s in model.by_type('IfcSystem'):
            cat = from_system(s.Name)
            if cat:
                for r in s.IsGroupedBy or []:
                    for o in r.RelatedObjects:
                        if hasattr(o, 'GlobalId'): systems[o.GlobalId] = cat

    def psets_of(el):
        out = {}
        try:
            for ps, props in uel.get_psets(el).items():
                p = {k: (round(v, 3) if isinstance(v, float) else v if isinstance(v, (int, str, bool)) else str(v))
                     for k, v in props.items() if k != 'id' and v not in (None, '')}
                if p: out[ps] = dict(list(p.items())[:20])
        except Exception: pass
        return dict(list(out.items())[:8])

    meta, dpos, didx, fpos, fidx, fids = [], [], [], [], [], []
    if it.initialize():
        while True:
            sh = it.get(); el = model.by_id(sh.id); cls = el.is_a()
            if cls not in SKIP:
                g = sh.geometry
                v = np.array(g.verts, dtype=np.float64).reshape(-1, 3); f = np.array(g.faces, dtype=np.int64).reshape(-1, 3)
                if len(f):
                    if origin is None:
                        origin = v.mean(0).round(0); json.dump(origin.tolist(), open(origin_file, 'w'))
                    v = (v - origin).astype(np.float32)
                    v = np.stack([v[:, 0], v[:, 2], -v[:, 1]], axis=1)  # IFC Z-up -> viewer Y-up
                    if clash_set is None or cls in clash_set:
                        fids.append(len(meta)); fpos.append(v); fidx.append(f.astype(np.uint32))
                    dv, df = v, f
                    if len(f) > MAX_TRIS:
                        try:
                            v2, f2 = fast_simplification.simplify(v, f.astype(np.int32), target_reduction=1 - MAX_TRIS / len(f))
                            if len(f2) >= 12: dv, df = v2.astype(np.float32), f2
                        except Exception: pass
                    if len(dv) <= 65535:
                        t = uel.get_type(el); c = uel.get_container(el)
                        meta.append({'guid': el.GlobalId, 'cls': cls, 'name': el.Name, 'type': t.Name if t else None,
                                     'storey': c.Name if c and c.is_a('IfcBuildingStorey') else None, 'disc': disc,
                                     'sys': systems.get(el.GlobalId), 'psets': psets_of(el),
                                     'vn': int(len(dv)), 'tn': int(len(df) * 3),
                                     'bmin': dv.min(0).round(3).tolist(), 'bmax': dv.max(0).round(3).tolist()})
                        dpos.append(dv.astype(np.float32)); didx.append(df.astype(np.uint16).ravel())
                    elif fids and fids[-1] == len(meta):
                        fids.pop(); fpos.pop(); fidx.pop()
            if not it.next(): break
    # storey elevations in viewer coordinates, for automatic levels
    storeys = []
    scale = uun.calculate_unit_scale(model)
    for s in model.by_type('IfcBuildingStorey'):
        try: z = upl.get_local_placement(s.ObjectPlacement)[2][3] * scale
        except Exception: z = (s.Elevation or 0) * scale
        storeys.append({'name': s.Name, 'y': float(z - origin[2]) if origin is not None else float(z),
                        'elev': float(s.Elevation) * scale if s.Elevation is not None else None})
    if not meta: raise SystemExit(f'{disc}: no geometry found in {path}')
    np.savez(os.path.join(work, f'{disc}.npz'), pos=np.concatenate(dpos), idx=np.concatenate(didx))
    if fids:
        vo = np.cumsum([0] + [len(p) for p in fpos]); to = np.cumsum([0] + [len(x) for x in fidx])
        np.savez(os.path.join(work, f'{disc}_full.npz'), ids=np.array(fids), pos=np.concatenate(fpos), idx=np.concatenate(fidx), voff=vo, toff=to)
    json.dump({'meta': meta, 'storeys': storeys}, open(os.path.join(work, f'{disc}.json'), 'w'), separators=(',', ':'))
    print(f'  {disc}: {len(meta)} elements, {sum(m["tn"] for m in meta)//3:,} triangles, {time.time()-t0:.0f}s', flush=True)


# ------------------------------------------------------------------ stage 2: clash detection on full-resolution meshes
def clashes(work, discs):
    import fcl
    data = {}
    for d in discs:
        p = os.path.join(work, f'{d}_full.npz')
        if not os.path.exists(p): continue
        z = np.load(p); meta = json.load(open(os.path.join(work, f'{d}.json')))['meta']
        ids, pos, idx, voff, toff = z['ids'], z['pos'], z['idx'], z['voff'], z['toff']  # .npz reloads on every access; read once
        items = []
        for n, i in enumerate(ids):
            v = pos[voff[n]:voff[n + 1]].astype(np.float64); f = idx[toff[n]:toff[n + 1]].reshape(-1, 3).astype(np.int64)
            items.append({'i': int(i), 'cls': meta[i]['cls'], 'v': v, 'f': f, 'lo': v.min(0), 'hi': v.max(0)})
        data[d] = items
    mep = [d for d in MEP if d in data]
    pairs = [(a, b) for a in mep for b in ('struct', 'arch') if b in data] + [(a, b) for n, a in enumerate(mep) for b in mep[n + 1:]]

    def bvh(v, f):
        m = fcl.BVHModel(); m.beginModel(len(v), len(f)); m.addSubModel(v, f); m.endModel()
        return fcl.CollisionObject(m, fcl.Transform())

    def shrunk(v, f):
        n = np.zeros_like(v); fn = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
        for k in range(3): np.add.at(n, f[:, k], fn)
        l = np.linalg.norm(n, axis=1, keepdims=True); l[l == 0] = 1
        return v - n / l * SHRINK

    objs, out = {}, []
    req = fcl.CollisionRequest(num_max_contacts=8, enable_contact=True)
    for a, b in pairs:
        A = [x for x in data[a] if not (a == 'elec' and x['cls'] == 'IfcBuildingElementProxy')]
        B = data[b]; blo = np.array([x['lo'] for x in B]); bhi = np.array([x['hi'] for x in B]); n = 0
        for x in A:
            hit = np.where(np.all(blo <= x['hi'], 1) & np.all(bhi >= x['lo'], 1))[0]
            if not len(hit): continue
            ka = (a, x['i'])
            if ka not in objs: objs[ka] = bvh(shrunk(x['v'], x['f']), x['f'])
            for h in hit:
                y = B[h]; kb = (b, y['i'], 'full')
                if kb not in objs: objs[kb] = bvh(y['v'], y['f'])
                res = fcl.CollisionResult()
                if fcl.collide(objs[ka], objs[kb], req, res):
                    pts = np.array([c.pos for c in res.contacts]) if res.contacts else ((np.maximum(x['lo'], y['lo']) + np.minimum(x['hi'], y['hi'])) / 2)[None]
                    ov = np.minimum(x['hi'], y['hi']) - np.maximum(x['lo'], y['lo'])
                    out.append({'a': [a, x['i']], 'b': [b, y['i']], 'kind': 'penetration' if y['cls'] in PENETRATION else 'hard',
                                'pair': f'{a} vs {b}', 'depth': round(float(ov.min()), 3), 'at': pts.mean(0).round(3).tolist()})
                    n += 1
        print(f'  {a} vs {b}: {n} clashes', flush=True)
    return out


# ------------------------------------------------------------------ stage 3: levels, chunks, manifest, thumbnail
def cap(v, t, n):
    import fast_simplification
    f = t.reshape(-1, 3)
    if len(f) <= n: return v, t
    try:
        v2, f2 = fast_simplification.simplify(v, f.astype(np.int32), target_reduction=1 - n / len(f))
        if len(f2) >= 8: return v2.astype(np.float32), f2.astype(np.uint16).ravel()
    except Exception: pass
    return v, t

def build_levels(storeys):
    """Group storeys closer than LEVEL_GAP into levels. Returns [{id, label, y}] sorted by height."""
    pts = sorted({(round(s['y'], 2), s['name'], s.get('elev')) for s in storeys if s['name'] is not None}, key=lambda p: p[0])
    groups = []
    for y, name, elev in pts:
        if groups and y - groups[-1]['y'] < LEVEL_GAP: groups[-1]['names'].append(name); groups[-1]['elevs'].append(elev)
        else: groups.append({'y': y, 'names': [name], 'elevs': [elev]})
    return groups

def thumb_svg(els, lo, hi):
    """Plan-view thumbnail: structure and walls as a faint footprint, MEP in its system colours."""
    w, d = max(hi[0] - lo[0], 1e-3), max(hi[2] - lo[2], 1e-3)
    turn = d > w * 1.15  # draw long buildings sideways so they fill a landscape card
    if turn: w, d = d, w
    S = 300 / max(w, d * 1.6); W, H = w * S + 20, d * S + 20
    area = w * d
    def xy(x, z): return ((z - lo[2]) if turn else (x - lo[0])) * S + 10, ((x - lo[0]) if turn else (z - lo[2])) * S + 10
    under, over = [], []
    for e in els:
        col = CAT_COLOR.get(e['cat'])
        if not col: continue
        (x0, _, z0), (x1, _, z1) = e['bmin'], e['bmax']
        if (x1 - x0) * (z1 - z0) > area * 0.2: continue  # slabs, roofs and site-wide items would cover everything
        (px0, py0), (px1, py1) = xy(x0, z0), xy(x1, z1)
        r = f'<rect x="{min(px0, px1):.1f}" y="{min(py0, py1):.1f}" width="{max(abs(px1 - px0), 0.7):.1f}" height="{max(abs(py1 - py0), 0.7):.1f}" fill="{col}"/>'
        (under if e['cat'] in ('structure', 'wall') else over).append(r)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} {H:.0f}"><rect width="100%" height="100%" fill="#e9ecee"/>'
            f'<g fill-opacity="0.45">{"".join(under[:5000])}</g><g fill-opacity="0.85">{"".join(over[:6000])}</g></svg>')

def thumb_from_project(dest):
    """Rebuild thumb.svg from a packaged project (uses the level files, not the IFCs)."""
    man = json.load(open(os.path.join(dest, 'manifest.json'))); co = man.get('catOverride', {}); els = []
    for l in man['levels']:
        if l['id'] == 'S': continue
        ch = json.load(open(os.path.join(dest, 'levels', l['file'])))
        q = np.frombuffer(base64.b64decode(ch['pos']), np.uint16).reshape(-1, 3).astype(np.float32)
        P = np.array(ch['lo'], np.float32) + q / 65535 * (np.array(ch['hi'], np.float32) - np.array(ch['lo'], np.float32))
        for e in ch['els']:
            v = P[e['v0']:e['v0'] + e['vn']]
            els.append({'cat': co.get(str(e['i']), {'duct': 'hvac', 'pipe': 'plumbother'}.get(e['cat'], e['cat'])), 'bmin': v.min(0).tolist(), 'bmax': v.max(0).tolist()})
    lo, hi = man['building']
    open(os.path.join(dest, 'thumb.svg'), 'w').write(thumb_svg(els, lo, hi))

def package(work, discs, dest, pid, name, container, clash_list):
    b64 = lambda a: base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()
    els, storeys = [], []
    for d in discs:
        j = json.load(open(os.path.join(work, f'{d}.json'))); z = np.load(os.path.join(work, f'{d}.npz'))
        if d != 'site': storeys += j['storeys']
        P, I = z['pos'], z['idx']; vo = to = 0
        for k, m in enumerate(j['meta']):
            v, t = P[vo:vo + m['vn']], I[to:to + m['tn']]; vo += m['vn']; to += m['tn']
            m['local'] = k
            m['cat'] = ('site' if d == 'site' else 'structure' if d == 'struct' else arch_category(m['cls']) if d == 'arch'
                        else (m.get('sys') or by_type(d, m['type'], m['name'])))
            v, t = cap(v, t, CAP.get(m['cat'], 300)); m['_v'], m['_t'], m['vn'], m['tn'] = v, t, len(v), len(t)
            els.append(m)
    groups = build_levels(storeys)
    # datum: the level nearest the model's zero becomes floor height 0 in the viewer
    # the storey the model itself puts at elevation 0 (the project's ground floor); fall back to the lowest level
    zero = [(abs(e), g) for g in groups for e in g['elevs'] if e is not None]
    y0 = min(zero, key=lambda t: t[0])[1]['y'] if zero else (groups[0]['y'] if groups else 0.0)
    if zero:  # the ground-floor storey's own height, not the group's lowest member
        y0 = next(s['y'] for s in storeys if s.get('elev') is not None and abs(s['elev']) == min(t[0] for t in zero))
    for g in groups: g['y'] -= y0
    storey_group = {}
    for n, g in enumerate(groups):
        for nm in g['names']: storey_group.setdefault(nm, n)
    for m in els:
        m['_v'] = m['_v'] - np.array([0, y0, 0], np.float32); m['bmin'][1] -= y0; m['bmax'][1] -= y0
        if m['disc'] == 'site' or not groups: m['lev'] = 'S' if m['disc'] == 'site' else 'L0'; continue
        n = storey_group.get(m['storey'])
        if n is None:
            yb = m['bmin'][1] + 0.1
            n = max([k for k, g in enumerate(groups) if g['y'] - 0.5 <= yb] or [0])
        m['lev'] = f'L{n}'
    level_defs = [(f'L{n}', ' / '.join(dict.fromkeys(g['names']))[:40], g['y']) for n, g in enumerate(groups)] or [('L0', 'Building', 0.0)]
    level_defs += [('S', 'Site', None)]
    order = {lid: n for n, (lid, _, _) in enumerate(level_defs)}
    els.sort(key=lambda m: (order[m['lev']], ORDER.index(m['disc'])))
    gid = {}
    for i, m in enumerate(els): m['i'] = i; gid[(m['disc'], m['local'])] = i
    bld = [m for m in els if m['disc'] != 'site'] or els
    lo = np.min([m['bmin'] for m in els], 0); hi = np.max([m['bmax'] for m in els], 0)
    blo = np.min([m['bmin'] for m in bld], 0); bhi = np.max([m['bmax'] for m in bld], 0)
    os.makedirs(os.path.join(dest, 'levels'), exist_ok=True)
    levels = []
    for lid, label, y in level_defs:
        L = [m for m in els if m['lev'] == lid]
        if not L: continue
        V = np.concatenate([m['_v'] for m in L]); clo, chi = V.min(0), V.max(0); span = np.maximum(chi - clo, 1e-6)
        q = np.round((V - clo) / span * 65535).astype(np.uint16); T = np.concatenate([m['_t'] for m in L]).astype(np.uint16)
        v0 = t0 = 0; out = []
        for m in L:
            out.append({'i': m['i'], 'guid': m['guid'], 'cls': m['cls'], 'name': m['name'], 'type': m['type'], 'storey': m['storey'],
                        'disc': m['disc'], 'cat': m['cat'], 'psets': m['psets'], 'v0': v0, 'vn': m['vn'], 't0': t0, 'tn': m['tn']})
            v0 += m['vn']; t0 += m['tn']
        fn = f'level-{lid}.json'
        s = json.dumps({'level': lid, 'lo': clo.tolist(), 'hi': chi.tolist(), 'els': out, 'pos': b64(q), 'idx': b64(T)}, separators=(',', ':'))
        open(os.path.join(dest, 'levels', fn), 'w').write(s)
        levels.append({'id': lid, 'label': label, 'y': None if y is None else round(y, 2), 'file': fn, 'bytes': len(s), 'n': len(L),
                       'tris': int(len(T) // 3), 'i0': L[0]['i'], 'discs': sorted({m['disc'] for m in L}, key=ORDER.index)})
        print(f'  {label}: {len(L)} elements, {len(T)//3:,} triangles, {len(s)/1e6:.1f} MB', flush=True)
    nm = lambda e: (e['name'] or e['cls']).split(':')[0][:48]
    clist = []
    for c in clash_list:
        a, b = gid.get(tuple(c['a'])), gid.get(tuple(c['b']))
        if a is None or b is None: continue
        ea, eb = els[a], els[b]; at = [c['at'][0], c['at'][1] - y0, c['at'][2]]
        clist.append({'a': a, 'b': b, 'kind': c['kind'], 'pair': c['pair'].replace('fire', 'fire'), 'depth': c['depth'], 'at': [round(x, 3) for x in at],
                      'la': ea['lev'], 'lb': eb['lev'], 'an': nm(ea), 'bn': nm(eb), 'ag': ea['guid'], 'bg': eb['guid'],
                      'loc': ea['storey'] or eb['storey'], 'ac': ea['cls'], 'bc': eb['cls']})
    counts = collections.Counter(m['cat'] for m in els)
    man = {'title': name, 'slug': re.sub(r'[^A-Za-z0-9]+', '-', name).strip('-')[:40], 'name': f'{name}: ' + ', '.join(d.capitalize() for d in discs),
           'info': {'container': container}, 'count': len(els), 'bounds': [lo.round(2).tolist(), hi.round(2).tolist()],
           'building': [blo.round(2).tolist(), bhi.round(2).tolist()], 'levels': levels, 'clashes': clist,
           'origin': json.load(open(os.path.join(work, 'origin.json'))), 'y0': y0,
           'catOverride': {m['i']: m['cat'] for m in els if m['disc'] in MEP}, 'catCounts': dict(counts)}
    json.dump(man, open(os.path.join(dest, 'manifest.json'), 'w'), separators=(',', ':'))
    open(os.path.join(dest, 'thumb.svg'), 'w').write(thumb_svg(els, blo, bhi))
    hard = sum(c['kind'] == 'hard' for c in clist)
    return {'id': pid, 'title': name, 'container': container, 'disciplines': discs, 'elements': len(els), 'levels': len([l for l in levels if l['id'] != 'S']),
            'hard': hard, 'penetrations': len(clist) - hard, 'triangles': sum(l['tris'] for l in levels),
            'added': datetime.date.today().isoformat()}
# ------------------------------------------------------------------ combined MEP files: split into trades so they clash against each other
TRADE = {'hvac': 'hvac', 'potable': 'plumb', 'sewer': 'plumb', 'gas': 'plumb', 'reclaimed': 'plumb', 'plumbother': 'plumb',
         'elec': 'elec', 'telecom': 'elec', 'fire': 'fire'}
BUILDING = {'IfcWall', 'IfcWallStandardCase', 'IfcSlab', 'IfcMember', 'IfcPlate', 'IfcCurtainWall', 'IfcBeam', 'IfcColumn', 'IfcDoor',
            'IfcWindow', 'IfcRoof', 'IfcStair', 'IfcStairFlight', 'IfcRamp', 'IfcRampFlight', 'IfcRailing', 'IfcCovering', 'IfcFooting',
            'IfcFurnishingElement', 'IfcFurniture', 'IfcPile', 'IfcChimney', 'IfcShadingDevice'}
MEP_WORDS = r'duct|diffuser|grille|register|vav|ahu|rtu|wshp|heat pump|fan|damper|air terminal|pipe|valve|pump|fixture|sink|lavatory|toilet|water|drain|conduit|cable|light|luminaire|panel|receptacle|switch|outlet|junction|transformer|sensor|data|tele|sprinkler|fire|alarm'
def mep_trade(m):
    if m.get('sys'): return TRADE[m['sys']]
    c, t = m['cls'], f"{m.get('type') or ''} {m.get('name') or ''}".lower()
    # combined MEP exports often carry the linked architecture; keep it as context, not as a trade
    if c in BUILDING: return 'arch'
    if c == 'IfcBuildingElementProxy' and not re.search(MEP_WORDS, t): return 'arch'
    if re.search(r'Duct|AirTerminal|Fan|Damper|UnitaryEquipment|Chiller|Boiler|Coil|AirToAir|Humidifier|Compressor|CoolingTower|Evaporat|Condenser', c) or re.search(r'duct|diffuser|grille|register|vav|ahu|rtu|fan|damper|air terminal', t): return 'hvac'
    if re.search(r'FireSuppression|Alarm', c) or re.search(r'sprinkler|fire', t): return 'fire'
    if re.search(r'Cable|Light|Lamp|Electric|Outlet|Switching|JunctionBox|ProtectiveDevice|Transformer|MotorConnection|CommunicationsAppliance|AudioVisual', c) or re.search(r'conduit|cable|light|luminaire|panel|receptacle|switch|outlet|junction|data|tele', t): return 'elec'
    return 'plumb'

def split_mep(work, existing):
    j = json.load(open(os.path.join(work, 'mep.json'))); z = np.load(os.path.join(work, 'mep.npz'))
    P, I = z['pos'], z['idx']  # .npz reloads on every access; read once
    fp = os.path.join(work, 'mep_full.npz'); zf = np.load(fp) if os.path.exists(fp) else None
    if zf is not None: zf = {k: zf[k] for k in ('ids', 'pos', 'idx', 'voff', 'toff')}
    trades = [mep_trade(m) for m in j['meta']]
    vo = np.cumsum([0] + [m['vn'] for m in j['meta']]); to = np.cumsum([0] + [m['tn'] for m in j['meta']])
    full_of = {int(i): n for n, i in enumerate(zf['ids'])} if zf is not None else {}
    made = []
    for tr in ['arch', 'hvac', 'plumb', 'elec', 'fire']:
        idx = [k for k, t in enumerate(trades) if t == tr]
        if not idx: continue
        if tr == 'arch' and 'arch' in existing: print(f'  mep: {len(idx)} linked architecture elements skipped (arch= given)'); continue
        if tr in existing: raise SystemExit(f'mep= and {tr}= both given; the combined file already contains {tr}')
        meta = []
        for k in idx: m = dict(j['meta'][k]); m['disc'] = tr; meta.append(m)
        np.savez(os.path.join(work, f'{tr}.npz'), pos=np.concatenate([P[vo[k]:vo[k + 1]] for k in idx]),
                 idx=np.concatenate([I[to[k]:to[k + 1]] for k in idx]))
        if zf is not None:
            # linked architecture only clashes as walls and structure, same as a separate arch= file
            fk = [(n, full_of[k]) for n, k in enumerate(idx) if k in full_of and (tr != 'arch' or j['meta'][k]['cls'] in (WALLS | STRUCTURAL))]
            if fk:
                pv = [zf['pos'][zf['voff'][f]:zf['voff'][f + 1]] for _, f in fk]; ti = [zf['idx'][zf['toff'][f]:zf['toff'][f + 1]] for _, f in fk]
                np.savez(os.path.join(work, f'{tr}_full.npz'), ids=np.array([n for n, _ in fk]), pos=np.concatenate(pv), idx=np.concatenate(ti),
                         voff=np.cumsum([0] + [len(p) for p in pv]), toff=np.cumsum([0] + [len(t) for t in ti]))
        json.dump({'meta': meta, 'storeys': j['storeys']}, open(os.path.join(work, f'{tr}.json'), 'w'), separators=(',', ':'))
        made.append(tr); print(f'  mep -> {tr}: {len(idx)} elements' + (' (linked architecture)' if tr == 'arch' else ''), flush=True)
    return made


def register(site, entry):
    idx_path = os.path.join(site, 'projects', 'index.json')
    idx = json.load(open(idx_path)) if os.path.exists(idx_path) else {'projects': []}
    idx['projects'] = [p for p in idx['projects'] if p['id'] != entry['id']] + [entry]
    json.dump(idx, open(idx_path, 'w'), indent=1)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--_extract':
        return extract(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] == '1')
    ap = argparse.ArgumentParser(description='Add a project to a Field Walk site.', epilog='Example: python add_project.py --id duplex --name "Duplex" arch=A.ifc hvac=M.ifc')
    ap.add_argument('--id', required=True, help='short id used in the web address, e.g. snowdon')
    ap.add_argument('--name', required=True, help='project name shown on the launch page')
    ap.add_argument('--container', help='ISO 19650 container name (default built from the id)')
    ap.add_argument('--site', default=HERE, help='site folder holding index.html and projects/ (default: this folder)')
    ap.add_argument('files', nargs='+', help='discipline=file.ifc pairs')
    a = ap.parse_args()
    pid = re.sub(r'[^a-z0-9-]+', '-', a.id.lower()).strip('-')
    files = {}
    for f in a.files:
        if '=' not in f: raise SystemExit(f'"{f}" should look like arch=Model.ifc')
        k, p = f.split('=', 1); d = ALIASES.get(k.lower().replace(' ', ''))
        if not d: raise SystemExit(f'Unknown discipline "{k}". Use: {", ".join(sorted(set(ALIASES)))}')
        if not os.path.exists(p): raise SystemExit(f'File not found: {p}')
        files[d] = p
    discs = [d for d in ORDER + ['mep'] if d in files]
    container = a.container or f'{pid[:3].upper()}-DUL-ZZ-ZZ-M3-Z-0001'
    work = tempfile.mkdtemp(prefix='fieldwalk-')
    t0 = time.time()
    try:
        print(f'1/3 Reading geometry ({", ".join(discs)})', flush=True)
        for d in discs:  # one process per file keeps memory down on big models
            r = subprocess.run([sys.executable, os.path.abspath(__file__), '--_extract', d, files[d], work, '1' if 'struct' not in files else '0'])
            if r.returncode: raise SystemExit(f'Failed while reading {files[d]}')
        if 'mep' in discs:
            discs.remove('mep')
            made = set(split_mep(work, discs))
            discs = [d for d in ORDER if d in set(discs) | made]
        print('2/3 Finding clashes', flush=True)
        cl = clashes(work, discs)
        print('3/3 Packaging levels', flush=True)
        dest = os.path.join(a.site, 'projects', pid)
        if os.path.exists(dest): shutil.rmtree(dest)
        entry = package(work, discs, dest, pid, a.name, container, cl)
        register(a.site, entry)
        print(f'Done in {time.time()-t0:.0f}s: {entry["elements"]:,} elements, {entry["hard"]} hard clashes, {entry["penetrations"]} penetrations.')
        print(f'Upload the projects/ folder, then open viewer.html?p={pid} (it is also listed on the launch page).')
    finally:
        shutil.rmtree(work, ignore_errors=True)

if __name__ == '__main__':
    main()
