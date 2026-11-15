"""Down-convert a KiCad 10 project to KiCad 5 file formats and zip it for EasyEDA Standard.

EasyEDA Standard's KiCad importer only reads the old formats (legacy .sch + .lib, version-2017 .kicad_pcb).

    python to_kicad5.py <project_dir> <name> <out_dir>

Writes <out_dir>/<name>_easyeda.zip (schematic + symbols + PCB) and <name>_easyeda_pcb_only.zip.
What is lost: zone fills (EasyEDA rebuilds them), 3D models, DNP flags, PWR_FLAG symbols, footprint-only
rule areas, and "remove unused pad layers" (through-hole pads get copper on both sides again).
"""
import math, os, re, sys, zipfile, zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sexp
from sexp import child

SRC, NAME, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(OUT, exist_ok=True)


def sv(x):
    """Plain value of a token (string tuple or bare word)."""
    return x[1].replace('\\"', '"').replace('\\\\', '\\') if isinstance(x, tuple) else str(x)


def q(s):
    """Quote the way KiCad 5 does: only when needed."""
    if s == '' or re.search(r'[\s()"#%{}\']', s) or s[0] == '-':
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return s


def one(e, tag, default=None):
    c = child(e, tag)
    return c[0] if c else default


def has_flag(e, name):
    """True for both `name` as a bare token and `(name yes)`."""
    if name in [t for t in e if isinstance(t, str)]:
        return True
    c = one(e, name)
    return bool(c) and (len(c) == 1 or sv(c[1]) == 'yes')


def ts(ref):
    return '%08X' % zlib.crc32(ref.encode())


def out(x, ind=0):
    """Emit nested lists of ready-made tokens."""
    if all(not isinstance(e, list) for e in x):
        return '(' + ' '.join(x) + ')'
    s, flat = '(', True
    for i, e in enumerate(x):
        if isinstance(e, list):
            deep = any(isinstance(k, list) for k in e)
            if ind < 2 and (deep or e[0] in ('fp_line', 'pad', 'segment', 'via', 'net', 'xy')):
                s += '\n' + '  ' * (ind + 1) + out(e, ind + 1); flat = False
            else:
                s += ' ' + out(e, ind + 2)
        else:
            s += ('' if i == 0 else ' ') + e
    return s + ('\n' + '  ' * ind if not flat else '') + ')'


# --------------------------------------------------------------------------------------------- PCB
def convert_pcb():
    t = sexp.parse(open(f'{SRC}/{NAME}.kicad_pcb', encoding='utf-8').read())[0]

    def netname(e):
        n = one(e, 'net')
        if not n:
            return None
        nm = sv(n[-1])
        if nm == '' or nm.startswith('unconnected-'):
            return None
        return nm[1:] if nm.startswith('/') else nm

    names = set()

    def collect(x):
        for e in x:
            if isinstance(e, list):
                if e and e[0] in ('pad', 'segment', 'via', 'zone') and netname(e):
                    names.add(netname(e))
                collect(e)
    collect(t)
    nets = {n: i + 1 for i, n in enumerate(sorted(names))}

    def net(e, named=True):
        n = netname(e)
        if n is None:
            return None
        return ['net', str(nets[n]), q(n)] if named else ['net', str(nets[n])]

    def width(e):
        st = one(e, 'stroke')
        return ['width', str(one(st, 'width')[1])] if st else (one(e, 'width') or ['width', '0.12'])

    def filled(e):
        f = one(e, 'fill')
        return bool(f) and sv(f[1]) in ('yes', 'solid')

    def layer(e):
        return ['layer', q(sv(one(e, 'layer')[1]))]

    def effects(e):
        ef = one(e, 'effects')
        font = one(ef, 'font')
        size = one(font, 'size') or ['size', '1', '1']
        th = one(font, 'thickness') or ['thickness', '%g' % (float(size[1]) * 0.15)]
        r = ['effects', ['font', ['size', size[1], size[2]], ['thickness', th[1]]]]
        j = one(ef, 'justify')
        if j:
            r.append(['justify'] + [x for x in j[1:] if isinstance(x, str)])
        return r

    def text_at(e):
        return ['at'] + [x for x in one(e, 'at')[1:] if isinstance(x, str) and x != 'unlocked']

    def graphics(e, pre):
        """fp_/gr_ line, rect, circle, poly -> list of v5 items."""
        kind, L, W = e[0][3:], layer(e), width(e)
        if kind == 'line':
            return [[pre + 'line', one(e, 'start'), one(e, 'end'), L, W]]
        if kind == 'rect':
            (x1, y1), (x2, y2) = one(e, 'start')[1:3], one(e, 'end')[1:3]
            pts = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
            if filled(e):
                return [[pre + 'poly', ['pts'] + [['xy', a, b] for a, b in pts], L, W]]
            return [[pre + 'line', ['start', *pts[i]], ['end', *pts[(i + 1) % 4]], L, W] for i in range(4)]
        if kind == 'circle':
            c, en = one(e, 'center'), one(e, 'end')
            if filled(e):   # v5 has no filled circle: half radius with a fat pen covers the disc
                cx, cy, ex, ey = map(float, c[1:3] + en[1:3])
                r = math.hypot(ex - cx, ey - cy)
                return [[pre + 'circle', c, ['end', '%g' % (cx + r / 2), '%g' % cy], L, ['width', '%g' % r]]]
            return [[pre + 'circle', c, en, L, W]]
        if kind == 'poly':
            pts = [p for p in one(e, 'pts') if isinstance(p, list) and p[0] == 'xy']
            if filled(e):
                return [[pre + 'poly', ['pts'] + pts, L, W]]
            return [[pre + 'line', ['start', *pts[i][1:]], ['end', *pts[(i + 1) % len(pts)][1:]], L, W] for i in range(len(pts))]
        raise SystemExit(f'unsupported graphic {e[0]}')

    def zones(z):
        k = one(z, 'keepout')
        if k and not any(sv(one(k, w)[1]) == 'not_allowed' for w in ('tracks', 'vias', 'copperpour')):
            return []                      # footprint-only rule area: nothing v5 can express
        lay = [sv(x) for x in (one(z, 'layers') or one(z, 'layer'))[1:]]
        n = netname(z)
        cp = one(z, 'connect_pads')
        fill = one(z, 'fill')
        res = []
        for l in lay:
            r = ['zone', ['net', str(nets[n]) if n else '0'], ['net_name', q(n or '')], ['layer', l], ['tstamp', '0'],
                 ['hatch', 'edge', '0.508'],
                 ['connect_pads'] + [x for x in cp[1:] if isinstance(x, str)] + [one(cp, 'clearance') or ['clearance', '0.25']],
                 one(z, 'min_thickness') or ['min_thickness', '0.2']]
            if k:
                r.append(['keepout'] + [[w, sv(one(k, w)[1])] for w in ('tracks', 'vias', 'copperpour')])
            r.append(['fill'] + ([] if k else ['yes']) + [['arc_segments', '32'],
                     one(fill, 'thermal_gap') or ['thermal_gap', '0.5'],
                     one(fill, 'thermal_bridge_width') or ['thermal_bridge_width', '0.5']])
            r.append(['polygon', ['pts'] + [p for p in one(one(z, 'polygon'), 'pts') if isinstance(p, list)]])
            res.append(r)
        return res

    body, counts = [], dict(modules=0, drawings=0, tracks=0, zones=0)
    for e in t:
        if not isinstance(e, list):
            continue
        if e[0] == 'footprint':
            counts['modules'] += 1
            props = {sv(p[1]): p for p in child(e, 'property')}
            ref = sv(props['Reference'][2])
            m = ['module', q(sv(e[1])), layer(e), ['tedit', '5F000000'], ['tstamp', ts(ref)], one(e, 'at')]
            for tag in ('descr', 'tags'):
                if one(e, tag):
                    m.append([tag, q(sv(one(e, tag)[1]))])
            m.append(['path', '/' + ts(ref)])
            attr = [x for x in (one(e, 'attr') or [])[1:]]
            if 'smd' in attr:
                m.append(['attr', 'smd'])
            elif 'exclude_from_pos_files' in attr or 'board_only' in attr:
                m.append(['attr', 'virtual'])
            for kind, key in (('reference', 'Reference'), ('value', 'Value')):
                p = props[key]
                m.append(['fp_text', kind, q(sv(p[2])), text_at(p), layer(p)] + (['hide'] if has_flag(p, 'hide') else []) + [effects(p)])
            for c in e:
                if not isinstance(c, list):
                    continue
                if c[0] == 'fp_text':
                    txt = sv(c[2]).replace('${REFERENCE}', '%R').replace('${VALUE}', '%V')
                    m.append(['fp_text', 'user', q(txt), text_at(c), layer(c)] + (['hide'] if has_flag(c, 'hide') else []) + [effects(c)])
                elif c[0] in ('fp_line', 'fp_rect', 'fp_circle', 'fp_poly'):
                    m += graphics(c, 'fp_')
                elif c[0] == 'fp_arc':
                    raise SystemExit('fp_arc not handled')
                elif c[0] == 'pad':
                    p = ['pad', q(sv(c[1])), c[2], c[3], one(c, 'at'), one(c, 'size')]
                    if one(c, 'drill'):
                        p.append(one(c, 'drill'))
                    p.append(['layers'] + [sv(x) for x in one(c, 'layers')[1:]])
                    for tag in ('roundrect_rratio', 'zone_connect', 'options'):
                        if one(c, tag):
                            p.append(one(c, tag))
                    if one(c, 'primitives'):
                        pr = ['primitives']
                        for g in one(c, 'primitives')[1:]:
                            if not isinstance(g, list):
                                continue
                            if g[0] != 'gr_poly':
                                raise SystemExit(f'pad primitive {g[0]} not handled')
                            pr.append(['gr_poly', ['pts'] + [x for x in one(g, 'pts') if isinstance(x, list)], width(g)])
                        p.append(pr)
                    if net(c):
                        p.append(net(c))
                    m.append(p)
            body.append(m)
        elif e[0] in ('gr_line', 'gr_rect', 'gr_circle', 'gr_poly'):
            g = graphics(e, 'gr_'); counts['drawings'] += len(g); body += g
        elif e[0] == 'gr_text':
            counts['drawings'] += 1
            body.append(['gr_text', q(sv(e[1])), text_at(e), layer(e), effects(e)])
        elif e[0] == 'segment':
            counts['tracks'] += 1
            body.append(['segment', one(e, 'start'), one(e, 'end'), one(e, 'width'), layer(e), net(e, False) or ['net', '0']])
        elif e[0] == 'via':
            counts['tracks'] += 1
            body.append(['via', one(e, 'at'), one(e, 'size'), one(e, 'drill'), ['layers', 'F.Cu', 'B.Cu'], net(e, False) or ['net', '0']])
        elif e[0] == 'zone':
            z = zones(e); counts['zones'] += len(z); body += z
        elif e[0] in ('gr_arc', 'arc', 'dimension', 'group', 'image'):
            raise SystemExit(f'{e[0]} not handled')

    layers = [(0, 'F.Cu', 'signal'), (1, 'In1.Cu', 'signal'), (2, 'In2.Cu', 'signal'), (3, 'In3.Cu', 'signal'), (4, 'In4.Cu', 'signal'), (31, 'B.Cu', 'signal')] + [(32 + i, n, 'user') for i, n in enumerate(
        'B.Adhes F.Adhes B.Paste F.Paste B.SilkS F.SilkS B.Mask F.Mask Dwgs.User Cmts.User Eco1.User Eco2.User Edge.Cuts Margin B.CrtYd F.CrtYd B.Fab F.Fab'.split())]
    head = ['kicad_pcb', ['version', '20171130'], ['host', 'pcbnew', '"(5.1.12)-1"'],
            ['general', ['thickness', '1.6'], ['drawings', str(counts['drawings'])], ['tracks', str(counts['tracks'])],
             ['zones', '0'], ['modules', str(counts['modules'])], ['nets', str(len(nets) + 1)]],
            ['page', 'A4'],
            ['layers'] + [[str(i), n, k] for i, n, k in layers],
            ['setup', ['last_trace_width', '0.25'], ['trace_clearance', '0.2'], ['zone_clearance', '0.25'], ['zone_45_only', 'no'],
             ['trace_min', '0.2'], ['via_size', '0.6'], ['via_drill', '0.3'], ['via_min_size', '0.4'], ['via_min_drill', '0.2'],
             ['uvia_size', '0.3'], ['uvia_drill', '0.1'], ['uvias_allowed', 'no'], ['uvia_min_size', '0.2'], ['uvia_min_drill', '0.1'],
             ['edge_width', '0.05'], ['segment_width', '0.2'], ['pcb_text_width', '0.3'], ['pcb_text_size', '1.5', '1.5'],
             ['mod_edge_width', '0.12'], ['mod_text_size', '1', '1'], ['mod_text_width', '0.15'], ['pad_size', '1.524', '1.524'],
             ['pad_drill', '0.762'], ['pad_to_mask_clearance', '0'], ['aux_axis_origin', '0', '0'], ['visible_elements', 'FFFFFF7F']],
            ['net', '0', '""']] + [['net', str(i), q(n)] for n, i in nets.items()] + [
            ['net_class', 'Default', '"This is the default net class."', ['clearance', '0.2'], ['trace_width', '0.25'],
             ['via_dia', '0.6'], ['via_drill', '0.3'], ['uvia_dia', '0.3'], ['uvia_drill', '0.1']] + [['add_net', q(n)] for n in nets]]
    lines = ['(' + head[0] + ' ' + out(head[1]) + ' ' + out(head[2])]
    for x in head[3:] + body:
        lines.append('  ' + out(x, 1))
    lines.append(')')
    path = f'{OUT}/{NAME}.kicad_pcb'
    open(path, 'w', encoding='utf-8', newline='\n').write('\n'.join(lines) + '\n')
    print('pcb:', counts, 'nets', len(nets))
    return path


# ----------------------------------------------------------------------------------------- schematic
def mil(v):
    return int(round(float(v) / 0.0254))


ETYPE = dict(input='I', output='O', bidirectional='B', tri_state='T', passive='P', unspecified='U', power_in='W',
             power_out='w', open_collector='C', open_emitter='E', no_connect='N', free='U')
ORIENT = {0: 'R', 90: 'U', 180: 'L', 270: 'D'}
MATRIX = {0: (1, 0, 0, -1), 90: (0, -1, -1, 0), 180: (-1, 0, 0, 1), 270: (0, 1, 1, 0)}


def arc_points(s, m, e, n=10):
    (x1, y1), (x2, y2), (x3, y3) = s, m, e
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:
        return [s, e]
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1) + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3) + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1, am, a2 = (math.atan2(y - uy, x - ux) for x, y in (s, m, e))
    sweep = (a2 - a1) % (2 * math.pi)
    if (am - a1) % (2 * math.pi) > sweep:
        sweep -= 2 * math.pi
    return [(ux + r * math.cos(a1 + sweep * i / n), uy + r * math.sin(a1 + sweep * i / n)) for i in range(n + 1)]


def convert_sch():
    root = sexp.parse(open(f'{SRC}/{NAME}.kicad_sch', encoding='utf-8').read())[0]
    sheets = []
    for sh in child(root, 'sheet'):
        pr = {sv(p[1]): sv(p[2]) for p in child(sh, 'property')}
        sheets.append((pr['Sheetname'], pr['Sheetfile']))
    if not sheets:
        sheets = [('', f'{NAME}.kicad_sch')]
    PAPER = {'A4': (11693, 8268), 'A3': (16535, 11693), 'A2': (23386, 16535), 'A1': (33110, 23386)}
    lib = ['EESchema-LIBRARY Version 2.4', '#encoding utf-8']
    pins, body, OX, H, comps, nlab, nstub = {}, [], 0, 0, 0, 0, 0
    for sname, sfile in sheets:
        t = sexp.parse(open(f'{SRC}/{sfile}', encoding='utf-8').read())[0]
        s = body
        mx = lambda v, OX=OX: mil(v) + OX
        for lid, sy in {sv(x[1]): x for x in child(one(t, 'lib_symbols'), 'symbol')}.items():
            if lid == 'power:PWR_FLAG' or lid in pins:
                continue
            props = {sv(p[1]): sv(p[2]) for p in child(sy, 'property')}
            pn, pnum = one(sy, 'pin_names'), one(sy, 'pin_numbers')
            offset = mil(one(pn, 'offset')[1]) if pn and one(pn, 'offset') else 20
            draw, plist, units = [], [], 1
            for u in child(sy, 'symbol'):
                unit, conv = map(int, sv(u[1]).rsplit('_', 2)[1:])
                units = max(units, unit)
                for g in u[2:]:
                    if not isinstance(g, list):
                        continue
                    st = one(g, 'stroke')
                    th = mil(one(st, 'width')[1]) if st and one(st, 'width') else 0
                    f = one(g, 'fill')
                    fill = {'background': 'f', 'outline': 'F'}.get(sv(one(f, 'type')[1]) if f else 'none', 'N')
                    if g[0] == 'rectangle':
                        a, b = one(g, 'start'), one(g, 'end')
                        draw.append(f'S {mil(a[1])} {mil(a[2])} {mil(b[1])} {mil(b[2])} {unit} {conv} {th} {fill}')
                    elif g[0] in ('polyline', 'arc'):
                        if g[0] == 'arc':
                            pts = arc_points(*[tuple(map(float, one(g, k)[1:3])) for k in ('start', 'mid', 'end')])
                        else:
                            pts = [(p[1], p[2]) for p in one(g, 'pts') if isinstance(p, list)]
                        draw.append(f'P {len(pts)} {unit} {conv} {th} ' + ' '.join(f'{mil(x)} {mil(y)}' for x, y in pts) + f' {fill}')
                    elif g[0] == 'circle':
                        c = one(g, 'center')
                        draw.append(f'C {mil(c[1])} {mil(c[2])} {mil(one(g, "radius")[1])} {unit} {conv} {th} {fill}')
                    elif g[0] == 'pin':
                        at = one(g, 'at'); a = int(float(at[3])) % 360
                        nm = sv(one(g, 'name')[1]).replace(' ', '_') or '~'
                        nu = sv(one(g, 'number')[1])
                        shape = ' N' if has_flag(g, 'hide') else ''
                        draw.append(f'X {nm} {nu} {mil(at[1])} {mil(at[2])} {mil(one(g, "length")[1])} {ORIENT[a]} 50 50 {unit} {conv} {ETYPE[g[1]]}{shape}')
                        plist.append((mil(at[1]), mil(at[2]), a, nu, unit))
                    elif g[0] in ('text', 'bezier', 'text_box'):
                        pass
                    else:
                        raise SystemExit(f'symbol graphic {g[0]} not handled')
            pins[lid] = plist
            cname, ref = lid.replace(':', '_'), props.get('Reference', 'U')
            yn = lambda x: 'N' if x and has_flag(x, 'hide') else 'Y'
            lib += ['#', f'# {cname}', '#', f'DEF {cname} {ref} 0 {offset} {yn(pnum)} {yn(pn)} {units} F N',
                    f'F0 "{ref}" 0 100 50 H V C CNN', f'F1 "{cname}" 0 -100 50 H V C CNN', 'F2 "" 0 0 50 H I C CNN', 'F3 "" 0 0 50 H I C CNN',
                    'DRAW'] + draw + ['ENDDRAW', 'ENDDEF']
        labels = {}
        for kind in ('label', 'global_label', 'hierarchical_label'):
            for l in child(t, kind):
                at = one(l, 'at'); nm = sv(l[1])
                if kind == 'label' and sname:
                    nm = sname + '/' + nm           # local net: same name as on the board
                labels.setdefault((mx(at[1]), mil(at[2])), []).append([nm, int(float(at[3])) // 90 % 4, None])
        pin_ends, stubs = set(), []
        for c in child(t, 'symbol'):
            lid = sv(one(c, 'lib_id')[1])
            if lid == 'power:PWR_FLAG':
                continue
            comps += 1
            at = one(c, 'at'); X, Y, rot = mx(at[1]), mil(at[2]), int(float(at[3])) % 360
            a, b, cc, d = MATRIX[rot]
            mir = one(c, 'mirror')
            if mir:
                if sv(mir[1]) == 'y':
                    a, cc = -a, -cc
                else:
                    b, d = -b, -d
            props = child(c, 'property')
            pv = {sv(p[1]): sv(p[2]) for p in props}
            ref = pv['Reference']
            unit = int(sv(one(c, 'unit')[1])) if one(c, 'unit') else 1
            s += ['$Comp', f'L {lid} {ref}', f'U {unit} 1 {ts(ref)}', f'P {X} {Y}']
            order = ['Reference', 'Value', 'Footprint', 'Datasheet']
            extra = [p for p in props if sv(p[1]) not in order and sv(p[2]) and sv(p[1]) != 'Description']
            bykey = {sv(p[1]): p for p in props}
            for i, p in enumerate([bykey.get(k) for k in order] + extra):
                if p is None:
                    s.append(f'F {i} "" H {X} {Y} 50  0001 C CNN'); continue
                pa = one(p, 'at')
                ef = one(p, 'effects')
                hidden = has_flag(p, 'hide') or (ef and has_flag(ef, 'hide')) or i >= 2
                j = (one(ef, 'justify') if ef else None) or []
                hj = 'L' if 'left' in j else 'R' if 'right' in j else 'C'
                hv = 'H' if (int(float(pa[3])) + rot) % 180 == 0 else 'V'
                val = sv(p[2]).replace('"', "'")
                s.append(f'F {i} "{val}" {hv} {mx(pa[1])} {mil(pa[2])} 50  000{1 if hidden else 0} {hj} CNN' + (f' "{sv(p[1])}"' if i >= 4 else ''))
            s += [f'\t{unit}    {X} {Y}', f'\t{a}    {b}    {cc}    {d}  ', '$EndComp']
            for px, py, ang, _, pu in pins[lid]:
                if pu not in (0, unit):
                    continue
                ex, ey = X + a * px + b * py, Y + cc * px + d * py
                pin_ends.add((ex, ey))
                ox, oy = -round(math.cos(math.radians(ang))), -round(math.sin(math.radians(ang)))
                sx, sy_ = a * ox + b * oy, cc * ox + d * oy
                for l in labels.get((ex, ey), []):
                    if l[2] is None:
                        l[2] = (ex + 100 * sx, ey + 100 * sy_)
                        l[1] = {(1, 0): 0, (0, -1): 1, (-1, 0): 2, (0, 1): 3}[(sx, sy_)]
                        stubs.append((ex, ey) + l[2])
        stubs = sorted(set(stubs))
        at_end = {}
        for v in labels.values():
            for nm, _, pos in v:
                if pos:
                    at_end.setdefault(pos, set()).add(nm)
        bad = [e for e, n in at_end.items() if e in pin_ends or len(n) > 1]
        if bad:
            raise SystemExit(f'{sfile}: wire stubs would short at {bad}')
        loose = sorted({l[0] for v in labels.values() for l in v if not l[2]})
        if loose:
            print(f'  {sfile}: labels not on a pin (dropped):', loose[:12])
        labels = {k: [l for l in v if l[2]] for k, v in labels.items()}
        for x1, y1, x2, y2 in stubs:
            s += ['Wire Wire Line', f'\t{x1} {y1} {x2} {y2}']
        for (x, y), v in labels.items():
            for nm, o, pos in v:
                s += [f'Text Label {pos[0]} {pos[1]} {o}    50   ~ 0', nm]
        for n in child(t, 'no_connect'):
            at = one(n, 'at'); s.append(f'NoConn ~ {mx(at[1])} {mil(at[2])}')
        for j in child(t, 'junction'):
            at = one(j, 'at'); s.append(f'Connection ~ {mx(at[1])} {mil(at[2])}')
        for w in child(t, 'wire'):
            p = [x for x in one(w, 'pts') if isinstance(x, list)]
            s += ['Wire Wire Line', f'\t{mx(p[0][1])} {mil(p[0][2])} {mx(p[1][1])} {mil(p[1][2])}']
        for x in child(t, 'text'):
            at = one(x, 'at'); size = one(one(one(x, 'effects'), 'font'), 'size')
            s += [f'Text Notes {mx(at[1])} {mil(at[2])} 0    {mil(size[1])}   ~ 0', sv(x[1]).replace('\n', '\\n')]
        if sname:
            s += [f'Text Notes {OX + 500} 400 0    150  ~ 30', 'SHEET: ' + sname]
        nlab += sum(len(v) for v in labels.values()); nstub += len(stubs)
        pw, ph = PAPER[sv(one(t, 'paper')[1])]
        OX += pw; H = max(H, ph)
    lib += ['#', '#End Library']
    open(f'{OUT}/{NAME}-cache.lib', 'w', encoding='utf-8', newline='\n').write('\n'.join(lib) + '\n')
    head = ['EESchema Schematic File Version 4', 'EELAYER 30 0', 'EELAYER END', f'$Descr User {OX} {H}', 'encoding utf-8', 'Sheet 1 1',
            f'Title "{NAME}"', 'Date ""', 'Rev ""', 'Comp ""', 'Comment1 ""', 'Comment2 ""', 'Comment3 ""', 'Comment4 ""', '$EndDescr']
    open(f'{OUT}/{NAME}.sch', 'w', encoding='utf-8', newline='\n').write('\n'.join(head + body + ['$EndSCHEMATC']) + '\n')
    print('sch: sheets', len(sheets), 'components', comps, 'labels', nlab, 'stubs', nstub)


convert_pcb()
convert_sch()
open(f'{OUT}/{NAME}.pro', 'w', newline='\n').write('update=2021-01-01 00:00:00\nversion=1\nlast_client=kicad\n[general]\nversion=1\n')
with zipfile.ZipFile(f'{OUT}/{NAME}_easyeda.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for f in (f'{NAME}.pro', f'{NAME}.sch', f'{NAME}-cache.lib', f'{NAME}.kicad_pcb'):
        z.write(f'{OUT}/{f}', f)
with zipfile.ZipFile(f'{OUT}/{NAME}_easyeda_pcb_only.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    z.write(f'{OUT}/{NAME}.kicad_pcb', f'{NAME}.kicad_pcb')
print('wrote', f'{OUT}/{NAME}_easyeda.zip')
