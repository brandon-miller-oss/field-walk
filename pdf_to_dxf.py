#!/usr/bin/env python3
"""
Clash Course: convert CAD-exported PDF sheets to DXF (opens in AutoCAD, Revit, BricsCAD, QGIS).

  python pdf_to_dxf.py PlanSet.pdf --sheets A-101,A-102,S-101 --out dxf/
  python pdf_to_dxf.py PlanSet.pdf --pages 135-146 --scale "1/8" --out dxf/

Works on vector PDFs (exported from Revit, AutoCAD, Bluebeam). Scanned PDFs contain only an
image and need tracing instead (trace.html). Lines, arcs, curves, rectangles, fills and text are
kept. Layers are named by colour and pen weight, so walls, grids and annotation separate cleanly.
Coordinates are in feet at the sheet's drawing scale: detected from scale notes like
1/8" = 1'-0", or set with --scale (inches on paper per foot). With --scale paper, output is
in paper inches. Save the DXF as DWG from AutoCAD, or with the free ODA File Converter.

Requires: pip install pymupdf ezdxf
"""
import argparse, collections, json, math, os, re, sys, time
import pymupdf
import ezdxf
from ezdxf.enums import TextEntityAlignment

SCALE_RE = re.compile(r'(\d+(?:/\d+)?|\d+\s+\d+/\d+)\s*"\s*=\s*1\s*\'\s*-?\s*0\s*"?')
SHEET_RE = re.compile(r'^(?:[A-Z]{1,3}-?\d{1,3}(?:\.\d{1,2})?[A-Z]?)$')


def frac(s):
    s = s.strip()
    if ' ' in s: whole, f = s.split(None, 1); return float(whole) + frac(f)
    if '/' in s: a, b = s.split('/'); return float(a) / float(b)
    return float(s)


def detect_scale(page):
    """Most common 'x" = 1'-0"' note on the sheet, as inches of paper per foot."""
    text = page.get_text()
    hits = [frac(m.group(1)) for m in SCALE_RE.finditer(text.replace('\n', ' '))]
    # only standard architectural and engineering scales; anything else is a mis-read of nearby text
    STD = {1/64, 1/32, 1/16, 3/32, 1/8, 3/16, 1/4, 3/8, 1/2, 3/4, 1.0, 1.5, 3.0, 1/10, 1/20, 1/30, 1/40, 1/50, 1/60}
    hits = [h for h in hits if any(abs(h - s) < 1e-6 for s in STD)]
    if not hits: return None, 0
    c = collections.Counter(round(h, 5) for h in hits)
    best, n = c.most_common(1)[0]
    return best, n


def sheet_number(page):
    W, H = page.rect.width, page.rect.height
    words = [w for w in page.get_text('words') if w[0] > W * 0.86 and w[1] > H * 0.55 and SHEET_RE.match(w[4])]
    return max(words, key=lambda w: (w[3] - w[1], w[1]))[4] if words else ''


def bezier(p0, p1, p2, p3, n=8):
    out = []
    for i in range(1, n + 1):
        t = i / n; u = 1 - t
        out.append((u**3 * p0.x + 3 * u * u * t * p1.x + 3 * u * t * t * p2.x + t**3 * p3.x,
                    u**3 * p0.y + 3 * u * u * t * p1.y + 3 * u * t * t * p2.y + t**3 * p3.y))
    return out


def hexcol(c):
    if not c: return None
    return '%02X%02X%02X' % tuple(max(0, min(255, round(v * 255))) for v in c[:3])


def convert_page(page, path, scale_in_per_ft=None, label=''):
    """Write one page to DXF. scale_in_per_ft None = paper inches."""
    H = page.rect.height
    if scale_in_per_ft:
        k = 1.0 / 72.0 / scale_in_per_ft           # PDF points -> model feet
        units = 2                                  # feet
    else:
        k = 1.0 / 72.0                             # PDF points -> paper inches
        units = 1
    X = lambda x: x * k
    Y = lambda y: (H - y) * k
    doc = ezdxf.new('R2018', setup=True)
    doc.header['$INSUNITS'] = units
    msp = doc.modelspace()
    layers = {}

    def layer_for(stroke, width, fill=False, dashed=False):
        col = hexcol(stroke) or '000000'
        w = round((width or 0) * 100) / 100
        name = f"{'FILL' if fill else 'PDF'}_{col}_{str(w).replace('.', 'p')}{'_DASH' if dashed else ''}"
        if name not in layers:
            lay = doc.layers.add(name)
            rgb = tuple(int(col[i:i + 2], 16) for i in (0, 2, 4))
            lay.rgb = rgb
            # pen weight in mm, snapped to the nearest standard lineweight
            mm = max(0.0, (width or 0) * 25.4 / 72)
            std = [0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200, 211]
            lay.dxf.lineweight = min(std, key=lambda s: abs(s / 100 - mm))
            if dashed: lay.dxf.linetype = 'DASHED'
            layers[name] = 0
        layers[name] += 1
        return name

    stats = collections.Counter()
    white = lambda c: c is not None and min(c[:3]) > 0.95
    for d in page.get_drawings():
        stroke, fill, width = d.get('color'), d.get('fill'), d.get('width') or 0
        # white strokes and fills only mask other linework on paper (text backgrounds, tag fills); in CAD they become solid blobs
        if white(stroke): stroke = None
        if white(fill): fill = None
        if stroke is None and fill is None: continue
        dashed = bool(d.get('dashes')) and d.get('dashes') not in ('[] 0', '[]0')
        # collect connected runs of segments into polylines
        runs, cur = [], []
        def flush():
            nonlocal cur
            if len(cur) > 1: runs.append(cur)
            cur = []
        for it in d['items']:
            op = it[0]
            if op == 'l':
                a, b = it[1], it[2]
                if cur and abs(cur[-1][0] - a.x) < 1e-6 and abs(cur[-1][1] - a.y) < 1e-6: cur.append((b.x, b.y))
                else: flush(); cur = [(a.x, a.y), (b.x, b.y)]
            elif op == 'c':
                a = it[1]
                if not (cur and abs(cur[-1][0] - a.x) < 1e-6 and abs(cur[-1][1] - a.y) < 1e-6): flush(); cur = [(a.x, a.y)]
                cur.extend(bezier(*it[1:5]))
            elif op == 're':
                flush(); r = it[1]; runs.append([(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1), (r.x0, r.y1), (r.x0, r.y0)])
            elif op == 'qu':
                flush(); q = it[1]; runs.append([(q.ul.x, q.ul.y), (q.ur.x, q.ur.y), (q.lr.x, q.lr.y), (q.ll.x, q.ll.y), (q.ul.x, q.ul.y)])
        flush()
        if d.get('closePath'):
            runs = [r if r[0] == r[-1] else r + [r[0]] for r in runs]
        for r in runs:
            pts = [(X(x), Y(y)) for x, y in r]
            closed = len(pts) > 2 and abs(pts[0][0] - pts[-1][0]) < 1e-9 and abs(pts[0][1] - pts[-1][1]) < 1e-9
            if stroke is not None and d.get('type') in ('s', 'fs', None):
                lay = layer_for(stroke, width, dashed=dashed)
                if len(pts) == 2: msp.add_line(pts[0], pts[1], dxfattribs={'layer': lay}); stats['lines'] += 1
                else: msp.add_lwpolyline(pts[:-1] if closed else pts, close=closed, dxfattribs={'layer': lay}); stats['polylines'] += 1
            if fill is not None and d.get('type') in ('f', 'fs') and closed and len(pts) <= 400:
                lay = layer_for(fill, 0, fill=True)
                h = msp.add_hatch(dxfattribs={'layer': lay}); h.set_solid_fill(rgb=tuple(int(hexcol(fill)[i:i + 2], 16) for i in (0, 2, 4)))
                h.paths.add_polyline_path(pts[:-1], is_closed=True); stats['fills'] += 1
    # text
    if 'TEXT' not in doc.layers: doc.layers.add('PDF_TEXT', color=7)
    for b in page.get_text('dict')['blocks']:
        for line in b.get('lines', []):
            dx, dy = line.get('dir', (1, 0))
            rot = math.degrees(math.atan2(-dy, dx))
            for sp in line['spans']:
                t = sp['text'].strip()
                if not t: continue
                ox, oy = sp['origin']
                msp.add_text(t, height=sp['size'] * 0.72 * k, rotation=rot,
                             dxfattribs={'layer': 'PDF_TEXT', 'insert': (X(ox), Y(oy))})
                stats['text'] += 1
    doc.saveas(path)
    return stats, len(layers)


def parse_pages(spec, n):
    out = []
    for part in spec.split(','):
        part = part.strip()
        if '-' in part: a, b = part.split('-'); out += list(range(int(a), int(b) + 1))
        elif part: out.append(int(part))
    return [p for p in out if 1 <= p <= n]


def main():
    ap = argparse.ArgumentParser(description='Convert CAD-exported PDF sheets to DXF.')
    ap.add_argument('pdf')
    ap.add_argument('--pages', help='page numbers, e.g. 135-146,82')
    ap.add_argument('--sheets', help='sheet numbers from the title block, e.g. A-101,A-102,S-101')
    ap.add_argument('--scale', default='auto', help='auto (read the sheet scale note), paper, or inches per foot such as 1/8')
    ap.add_argument('--out', default='dxf')
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    pdf = pymupdf.open(a.pdf)
    pages = parse_pages(a.pages, len(pdf)) if a.pages else []
    if a.sheets:
        want = {s.strip().upper() for s in a.sheets.split(',')}
        print(f'Finding sheets {", ".join(sorted(want))}…', flush=True)
        for i, pg in enumerate(pdf):
            if sheet_number(pg).upper() in want: pages.append(i + 1)
    if not pages: raise SystemExit('No pages selected. Use --pages or --sheets.')
    log = []
    for n in sorted(set(pages)):
        t0 = time.time(); pg = pdf[n - 1]
        if not pg.get_drawings() and pg.get_images():
            print(f'  page {n}: scanned image only, skipped (trace it in trace.html)'); continue
        sh = sheet_number(pg) or f'page-{n:03d}'
        if a.scale == 'paper': sc, note = None, 'paper inches'
        elif a.scale == 'auto':
            sc, hits = detect_scale(pg)
            note = f'{sc}" = 1\'-0" (from {hits} scale note{"s" if hits != 1 else ""})' if sc else 'no scale note found: paper inches'
        else: sc = frac(a.scale); note = f'{a.scale}" = 1\'-0" (given)'
        path = os.path.join(a.out, f'{sh}.dxf')
        stats, nl = convert_page(pg, path, sc, sh)
        print(f'  {sh} (page {n}): {stats["lines"] + stats["polylines"]:,} linework, {stats["fills"]:,} fills, {stats["text"]:,} text, {nl} layers; {note}; {time.time() - t0:.0f}s', flush=True)
        log.append({'page': n, 'sheet': sh, 'file': os.path.basename(path), 'scale': note, **stats})
    json.dump(log, open(os.path.join(a.out, 'conversion-log.json'), 'w'), indent=1)
    print(f'Done: {len(log)} DXF files in {a.out}/')


if __name__ == '__main__':
    main()
