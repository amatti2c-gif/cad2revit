"""Cortes, fachadas y planta de techo de UN edificio.

- titulo_de(): qué es cada vista según su título ("CORTE A-A", "ELEVACIÓN NORTE", "PLANTA DE TECHO").
- marcas_corte(): en la planta, las letras de corte (A … A) y la línea que las une.
- perfil(): en un corte o fachada, la altura del alero, de la cumbrera y la pendiente.
- techo(): en la planta de techo, el contorno de la cubierta (con aleros) y las cumbreras.

Todo se mide en metros; las alturas son relativas al ±0.00 del dibujo (o a la línea de suelo).
"""
from __future__ import annotations

import math
import re
from statistics import median

from shapely.geometry import LineString, Polygon
from shapely.ops import polygonize, unary_union

from .dxf_reader import DibujoCAD
from .niveles import cota_en_texto

RX_TECHO = re.compile(r"(?i)planta\s+(de\s+)?(techo|techumbre|cubierta)s?|\btechumbre\b|\bplanta\s+techo")
RX_CORTE = re.compile(r"(?i)\b(?:corte|secci[oó]n|section)\s*([A-Z0-9]{1,2})(?:\s*[-–'´`]\s*([A-Z0-9]{1,2}))?")
RX_FACHADA = re.compile(r"(?i)\b(elevaci[oó]n|fachada|elevation)\b")
RX_PLANTA = re.compile(r"(?i)\bplanta\b|\bplan\b|\bnivel\s*\d")
RX_LETRA = re.compile(r"^\s*([A-Z0-9]{1,2})\s*$")
RX_COTA_DIM = re.compile(r"(?i)cota|dim|acot|texto|nota|anota")


def titulo_de(texto: str) -> tuple[str, str | None] | None:
    """(tipo, letra) si el texto es el título de una vista; None si no lo es."""
    t = texto.strip()
    if len(t) > 60:
        return None
    if RX_TECHO.search(t):
        return "techo", None
    m = RX_CORTE.search(t)
    if m:
        return "corte", m.group(1).upper()
    if RX_FACHADA.search(t):
        return "fachada", None
    if RX_PLANTA.search(t):
        return "planta", None
    return None


def titulos(dib: DibujoCAD, cajas: list[list[float]], debajo: float = 8.0, lado: float = 2.0) -> dict[int, tuple]:
    """Para cada caja (vista), el título más cercano: dentro de ella o hasta `debajo` m bajo ella."""
    out: dict[int, tuple[float, tuple, str]] = {}
    textos = [(t.punto, t.texto) for t in dib.textos] + \
             [(i.punto, v) for i in dib.inserciones for v in (i.atributos or {}).values()]
    for p, texto in textos:
        r = titulo_de(texto)
        if not r:
            continue
        mejor = None
        for k, c in enumerate(cajas):
            if not (c[0] - lado <= p[0] <= c[2] + lado and c[1] - debajo <= p[1] <= c[3] + lado):
                continue
            # distancia a la caja (0 si está dentro); preferir los que están bajo la vista
            dx = max(c[0] - p[0], 0, p[0] - c[2])
            dy = max(c[1] - p[1], 0, p[1] - c[3])
            d = math.hypot(dx, dy)
            if mejor is None or d < mejor[0]:
                mejor = (d, k)
        if mejor and (mejor[1] not in out or mejor[0] < out[mejor[1]][0]):
            out[mejor[1]] = (mejor[0], r, texto.strip())
    return {k: (v[1][0], v[1][1], v[2]) for k, v in out.items()}


def marcas_corte(dib: DibujoCAD, letras: set[str]) -> dict[str, tuple[tuple, tuple]]:
    """Letras de corte en la planta (dos marcas con la misma letra): letra -> (p1, p2) de la línea de corte."""
    puntos: dict[str, list[tuple]] = {}
    textos = [(t.punto, t.texto) for t in dib.textos] + \
             [(i.punto, v) for i in dib.inserciones for v in (i.atributos or {}).values()]
    for p, texto in textos:
        m = RX_LETRA.match(texto) or re.match(r"^\s*([A-Z0-9]{1,2})\s*[-–]\s*\1\s*$", texto)
        if m and m.group(1).upper() in letras:
            puntos.setdefault(m.group(1).upper(), []).append(p)
    # las letras de corte suelen repetir los nombres de los ejes (A, B, C…): se prefiere el par unido por la
    # línea de corte dibujada; si no, el par alineado en X o en Y; si no, el más lejano
    segs = [(s.a, s.b) for s in dib.segmentos if math.dist(s.a, s.b) >= 1.0]

    def unidos(p, q):
        for a, b in segs:
            if (math.dist(a, p) < 0.8 and math.dist(b, q) < 0.8) or (math.dist(a, q) < 0.8 and math.dist(b, p) < 0.8):
                return True
        return False

    out = {}
    for letra, ps in puntos.items():
        pares = [(p, q) for i, p in enumerate(ps) for q in ps[i + 1:] if math.dist(p, q) >= 1.0]
        if not pares:
            continue
        def nota(pq):
            p, q = pq
            alineado = min(abs(p[0] - q[0]), abs(p[1] - q[1])) < 0.2
            return (unidos(p, q), alineado, math.dist(p, q))
        a, b = max(pares, key=nota)
        out[letra] = (tuple(a), tuple(b))
    return out


def aguas(dib: DibujoCAD, caja) -> list[tuple[float, float, float]]:
    """Frontones simétricos (/\\) dibujados en un corte o fachada: (ancho horizontal, pendiente en grados,
    cota Y de la cumbrera en el dibujo).
    Sirven para saber la pendiente de cada cuerpo del techo según su ancho."""
    x0, y0, x1, y1 = caja
    inc = []
    for s in dib.segmentos:
        if not all(x0 - 0.01 <= q[0] <= x1 + 0.01 and y0 - 0.01 <= q[1] <= y1 + 0.01 for q in (s.a, s.b)):
            continue
        dx, dy = s.b[0] - s.a[0], s.b[1] - s.a[1]
        L = math.hypot(dx, dy)
        if L < 0.8 or not 0.1 < abs(dy) / L < 0.95:
            continue
        alto, bajo = (s.a, s.b) if s.a[1] > s.b[1] else (s.b, s.a)
        inc.append((alto, bajo, math.degrees(math.atan2(abs(dy), abs(dx)))))
    out = set()
    for i, (t1, b1, g1) in enumerate(inc):
        for t2, b2, g2 in inc[i + 1:]:
            if math.dist(t1, t2) > 0.03 or abs(g1 - g2) > 0.5:
                continue
            w1, w2 = b1[0] - t1[0], b2[0] - t2[0]
            if w1 * w2 >= 0 or abs(abs(w1) - abs(w2)) > 0.15:
                continue                                  # mismo lado, o asimétrico
            out.add((round(abs(w1) + abs(w2), 2), round((g1 + g2) / 2, 1), round(t1[1], 3), round(t1[0], 3)))
    # de las líneas paralelas de un mismo techo (cubierta, costanera, cielo…) queda solo la de más arriba
    exterior = [f for f in out if not any(abs(o[3] - f[3]) < 0.05 and abs(o[1] - f[1]) < 0.5 and o[2] > f[2] + 1e-3
                                          for o in out)]
    return sorted((w, g, y) for w, g, y, _ in exterior)


def _base(dib: DibujoCAD, caja, patron_nivel: str | None) -> float:
    """Cota 0 del dibujo: texto ±0.00, o la línea de suelo, o el borde inferior."""
    if patron_nivel:
        rx = re.compile(patron_nivel)
        ceros = []
        for t in dib.textos:
            v = cota_en_texto(t.texto, rx)
            if v is not None and abs(v) < 0.005:
                ceros.append(t.punto[1])
        if ceros:
            return median(ceros)
    ancho = caja[2] - caja[0]
    suelos = [min(s.a[1], s.b[1]) for s in dib.segmentos
              if abs(s.b[1] - s.a[1]) < 0.01 and abs(s.b[0] - s.a[0]) >= 0.7 * ancho]
    return min(suelos) if suelos else caja[1]


def perfil(dib: DibujoCAD, caja, patron_nivel: str | None = None) -> dict | None:
    """Alturas del contorno superior de una fachada o corte: alero (bordes), cumbrera (máximo) y pendiente."""
    segs = [s for s in dib.segmentos if not RX_COTA_DIM.search(s.capa)]
    if len(segs) < 3:
        return None
    base = _base(dib, caja, patron_nivel)
    # quedarse con el cuerpo del edificio: lo que se apoya en la línea de suelo
    x0, x1 = caja[0], caja[2]
    n = 80
    xs = [x0 + (x1 - x0) * (i + 0.5) / n for i in range(n)]
    tope = []
    for x in xs:
        ys = []
        for s in segs:
            (ax, ay), (bx, by) = s.a, s.b
            if min(ax, bx) - 1e-6 <= x <= max(ax, bx) + 1e-6:
                ys.append(max(ay, by) if abs(bx - ax) < 1e-9 else ay + (by - ay) * (x - ax) / (bx - ax))
        tope.append(max(ys) - base if ys else None)
    validos = [(x, y) for x, y in zip(xs, tope) if y is not None and y > 0.5]
    if len(validos) < 6:
        return None
    def tope_en(x):
        ys = []
        for s in segs:
            (ax, ay), (bx, by) = s.a, s.b
            if min(ax, bx) - 1e-6 <= x <= max(ax, bx) + 1e-6:
                ys.append(max(ay, by) if abs(bx - ax) < 1e-9 else ay + (by - ay) * (x - ax) / (bx - ax))
        return max(ys) - base if ys else None

    alturas = [y for _, y in validos]
    cumbrera = sorted(alturas)[int(0.95 * (len(alturas) - 1))]
    i_max = max(range(len(validos)), key=lambda i: validos[i][1])
    x_cum = validos[i_max][0]
    # afinar: el vértice más alto cerca del máximo muestreado (la cumbrera exacta)
    paso = (x1 - x0) / n * 1.5
    cerca = [(p[1] - base, p[0]) for s in segs for p in (s.a, s.b) if abs(p[0] - x_cum) <= paso]
    if cerca:
        y_c, x_c = max(cerca)
        if y_c >= cumbrera and y_c - cumbrera < 0.5:
            cumbrera, x_cum = y_c, x_c
    # muros exteriores: verticales que nacen del suelo; el alero se mide sobre ellos (no en la punta del alero)
    muros = [s.a[0] for s in segs if abs(s.b[0] - s.a[0]) < 0.01 and abs(s.b[1] - s.a[1]) > 1.0
             and min(s.a[1], s.b[1]) - base < 0.15]
    if len(muros) >= 2 and max(muros) - min(muros) > 1.0:
        xl, xr = min(muros), max(muros)
        ai, ad = tope_en(xl), tope_en(xr)
    else:
        borde = max(1, len(validos) // 12)
        xl, xr = validos[0][0], validos[-1][0]
        ai, ad = min(y for _, y in validos[:borde]), min(y for _, y in validos[-borde:])
    ai, ad = ai if ai is not None else cumbrera, ad if ad is not None else cumbrera
    alero = max(ai, ad) if abs(ai - ad) < 0.3 else min(ai, ad)
    run = max(min(x_cum - xl, xr - x_cum), 0.0)
    if run < 0.5:                                    # cumbrera en un extremo: techo de una agua
        run = xr - xl
    alto = cumbrera - alero
    pendiente = math.degrees(math.atan2(alto, run)) if alto > 0.15 and run > 0 else 0.0
    return {"base": round(base, 3), "alero": round(alero, 2), "cumbrera": round(cumbrera, 2),
            "pendiente": round(pendiente, 1), "ancho": round(xr - xl, 2)}


def techo(dib: DibujoCAD) -> dict | None:
    """Contorno de la cubierta (polígono más grande que forman las líneas) y cumbreras (líneas largas interiores)."""
    lineas = [LineString([s.a, s.b]) for s in dib.segmentos if math.dist(s.a, s.b) > 0.05]
    if len(lineas) < 3:
        return None
    try:
        polis = list(polygonize(unary_union(lineas)))
    except Exception:
        polis = []
    if polis:
        unido = unary_union(polis)
        poli = max(getattr(unido, "geoms", [unido]), key=lambda g: g.area)
        poli = Polygon(poli.exterior).simplify(0.05)
    else:
        xs = [p[0] for s in dib.segmentos for p in (s.a, s.b)]
        ys = [p[1] for s in dib.segmentos for p in (s.a, s.b)]
        poli = Polygon([(min(xs), min(ys)), (max(xs), min(ys)), (max(xs), max(ys)), (min(xs), max(ys))])
    if poli.area < 4:
        return None
    borde = poli.exterior
    mayor = max(poli.bounds[2] - poli.bounds[0], poli.bounds[3] - poli.bounds[1])
    cumbreras, limatones = [], []
    for s in dib.segmentos:
        L = math.dist(s.a, s.b)
        if L < 0.25 * mayor:
            continue
        medio = ((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2)
        from shapely.geometry import Point
        if not poli.buffer(-0.2).contains(Point(medio)):
            continue                                      # está sobre el borde o afuera
        ang = math.degrees(math.atan2(s.b[1] - s.a[1], s.b[0] - s.a[0])) % 180
        recta = min(ang, abs(ang - 90), 180 - ang) < 5
        # limatón: línea diagonal que llega a una esquina del contorno
        toca = min(borde.distance(Point(s.a)), borde.distance(Point(s.b))) < 0.3
        if recta and not toca:
            cumbreras.append((tuple(s.a), tuple(s.b)))
        elif not recta and toca:
            limatones.append((tuple(s.a), tuple(s.b)))
        elif recta:
            cumbreras.append((tuple(s.a), tuple(s.b)))
    coords = list(poli.exterior.coords)[:-1]
    if Polygon(coords).exterior.is_ccw is False:
        coords.reverse()
    return {"contorno": [[round(x, 3), round(y, 3)] for x, y in coords],
            "cumbreras": cumbreras, "limatones": len(limatones),
            "lineas": [(tuple(s.a), tuple(s.b)) for s in dib.segmentos if math.dist(s.a, s.b) > 0.4]}


def cubierta(contorno: list, cumbreras: list, limatones: int, pendiente: float,
             dir_cumbrera: tuple | None = None) -> dict:
    """Qué bordes del contorno llevan pendiente: dos aguas (bordes paralelos a la cumbrera),
    cuatro aguas (todos, si hay limatones) o plana."""
    if pendiente < 3:
        return {"forma": "plana", "bordes_pendiente": []}
    if limatones >= 2:
        return {"forma": "cuatro aguas", "bordes_pendiente": list(range(len(contorno)))}
    if cumbreras:
        a, b = max(cumbreras, key=lambda c: math.dist(*c))
        d = (b[0] - a[0], b[1] - a[1])
    elif dir_cumbrera:
        d = dir_cumbrera
    else:                                            # sin datos: cumbrera a lo largo del lado mayor
        xs = [p[0] for p in contorno]
        ys = [p[1] for p in contorno]
        d = (1, 0) if max(xs) - min(xs) >= max(ys) - min(ys) else (0, 1)
    ang_c = math.atan2(d[1], d[0])
    bordes = []
    for i, p in enumerate(contorno):
        q = contorno[(i + 1) % len(contorno)]
        ang = math.atan2(q[1] - p[1], q[0] - p[0])
        dif = abs((ang - ang_c + math.pi / 2) % math.pi - math.pi / 2)
        if dif < math.radians(10) and math.dist(p, q) > 0.5:
            bordes.append(i)
    return {"forma": "dos aguas", "bordes_pendiente": bordes}


def bordes_por_achurado(contorno: list, lineas: list, adentro: float = 0.5, radio: float = 0.35) -> list:
    """Para cada borde del contorno del techo: True si lleva pendiente, False si es frontón, None si no se sabe.
    Se mira el achurado de la planta de techo justo adentro del borde: las planchas (y su achurado) van en el
    sentido en que escurre el agua, así que si llegan perpendiculares al borde el agua cae por ahí (pendiente);
    si corren paralelas al borde, es un frontón."""
    from shapely.geometry import LineString, Point, Polygon
    poli = Polygon(contorno)
    if not poli.is_valid or poli.area <= 0:
        return [None] * len(contorno)
    segs = [(a, b, LineString([a, b])) for a, b in lineas if math.dist(a, b) > 0.4]
    out = []
    n = len(contorno)
    for i in range(n):
        p, q = contorno[i], contorno[(i + 1) % n]
        L = math.dist(p, q)
        if L < 0.3:
            out.append(None)
            continue
        ux, uy = (q[0] - p[0]) / L, (q[1] - p[1]) / L
        votos = {"pendiente": 0.0, "fronton": 0.0}
        for f in (0.25, 0.5, 0.75):                    # tres puntos a lo largo del borde
            mx, my = p[0] + ux * L * f, p[1] + uy * L * f
            # hacia adentro: probar ambos lados de la normal
            punto = None
            for sx, sy in ((-uy, ux), (uy, -ux)):
                cand = Point(mx + sx * adentro, my + sy * adentro)
                if poli.contains(cand):
                    punto = cand
                    break
            if punto is None:
                continue
            cerca = [(a, b) for a, b, ls in segs if ls.distance(punto) <= radio]
            if len(cerca) < 3:
                continue                                # sin achurado aquí
            # familia dominante (por ángulo, ponderada por largo)
            bins: dict[int, float] = {}
            for a, b in cerca:
                ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180
                k = int(round(ang / 5)) % 36
                bins[k] = bins.get(k, 0.0) + math.dist(a, b)
            k = max(bins, key=bins.get)
            if sum(1 for a, b in cerca if int(round((math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180) / 5)) % 36 == k) < 3:
                continue
            ang_h = math.radians(k * 5)
            coseno = abs(math.cos(ang_h) * ux + math.sin(ang_h) * uy)    # 1 = paralelo al borde, 0 = perpendicular
            if coseno < 0.5:
                votos["pendiente"] += 1
            elif coseno > 0.87:
                votos["fronton"] += 1
        if votos["pendiente"] > votos["fronton"]:
            out.append(True)
        elif votos["fronton"] > votos["pendiente"]:
            out.append(False)
        else:
            out.append(None)
    return out


def cuerpos(contorno: list, bordes: list, pendiente: float, cumbreras: list, frontones: list,
            cota_cubierta: float | None = None) -> dict | None:
    """Cuerpos del techo: los bordes con pendiente se agrupan por dirección (un cuerpo principal y, p. ej., un
    acceso perpendicular). En cada cuerpo:
      - la cumbrera es la de la planta de techo (si está dibujada) o la línea media entre los aleros extremos;
      - la pendiente es la del frontón de las elevaciones cuyo ancho calza con el del cuerpo (o la general);
      - todas las aguas llegan a la misma cumbrera: un alero más cercano a ella queda más alto (alturas_bordes).
    Devuelve, alineado con `bordes`: pendientes_bordes (grados), alturas_bordes (m sobre el nivel de cubierta),
    cuerpos_bordes (índice de cuerpo) y la lista de cuerpos (dirección, franja y tramo que cubren)."""
    n = len(contorno)
    if not bordes or n < 3:
        return None
    def direc(i):
        a, b = contorno[i], contorno[(i + 1) % n]
        L = math.dist(a, b) or 1.0
        return ((b[0] - a[0]) / L, (b[1] - a[1]) / L)

    grupos: list[list[int]] = []
    for i in sorted(bordes, key=lambda i: -math.dist(contorno[i], contorno[(i + 1) % n])):
        u = direc(i)
        for g in grupos:
            v = direc(g[0])
            if abs(u[0] * v[1] - u[1] * v[0]) < math.sin(math.radians(10)):
                g.append(i)
                break
        else:
            grupos.append([i])

    datos = {}
    lista = []
    for k, g in enumerate(grupos):
        u = direc(g[0])
        nrm = (-u[1], u[0])
        S = lambda p: nrm[0] * p[0] + nrm[1] * p[1]          # noqa: E731  (coordenada a través de la cumbrera)
        T = lambda p: u[0] * p[0] + u[1] * p[1]              # noqa: E731  (coordenada a lo largo)
        s_b = {i: (S(contorno[i]) + S(contorno[(i + 1) % n])) / 2 for i in g}
        s0, s1 = min(s_b.values()), max(s_b.values())
        ts = [T(contorno[j]) for i in g for j in (i, (i + 1) % n)]
        t0, t1 = min(ts), max(ts)
        # cumbrera dibujada en la planta de techo, paralela y dentro de la franja del cuerpo
        cands = []
        for a, b in cumbreras:
            L = math.dist(a, b)
            if L < 0.5 or abs((b[0] - a[0]) * nrm[0] + (b[1] - a[1]) * nrm[1]) / L > math.sin(math.radians(5)):
                continue
            sr = (S(a) + S(b)) / 2
            ta, tb = sorted((T(a), T(b)))
            if s0 + 0.3 < sr < s1 - 0.3 and min(tb, t1) - max(ta, t0) >= 0.5 * L:
                cands.append(sr)
        if cands and s1 - s0 > 0.6:
            sr = max(cands, key=lambda sr: min(sr - s0, s1 - sr))
        else:
            sr = (s0 + s1) / 2
        # pendiente: frontón de las elevaciones con el mismo ancho que el cuerpo
        ancho = s1 - s0
        cerca = sorted((abs(f[0] - ancho), -f[2] if len(f) > 2 and f[2] is not None else 0.0, f) for f in frontones
                       if abs(f[0] - ancho) <= 0.35)
        elegido = cerca[0][2] if cerca else None
        pend = elegido[1] if elegido else pendiente
        tg = math.tan(math.radians(pend))
        lados = {s_b[i] < sr for i in g}
        alto = max(tg * abs(s_b[i] - sr) for i in g)
        if elegido and len(elegido) > 2 and elegido[2] is not None and cota_cubierta is not None and len(lados) == 2:
            # altura de la cumbrera leída del frontón (sobre el nivel de cubierta): los aleros quedan bajo el
            # nivel lo que corresponde a su vuelo
            if 0.3 < elegido[2] - cota_cubierta < alto + 1.0:
                alto = elegido[2] - cota_cubierta
        for i in g:
            sube = alto - tg * abs(s_b[i] - sr) if len(lados) == 2 else 0.0
            datos[i] = (pend, round(sube, 3), k)
        lista.append({"direccion": [round(u[0], 6), round(u[1], 6)], "franja": [round(s0, 3), round(s1, 3)],
                      "tramo": [round(t0, 3), round(t1, 3)], "cumbrera": round(sr, 3), "pendiente": pend,
                      "alto": round(alto, 3)})
    # cada cuerpo se prolonga a lo largo hasta la cumbrera de otro cuerpo que lo cruza (allí se mete bajo él)
    for k, c in enumerate(lista):
        u = c["direccion"]
        for j, o in enumerate(lista):
            if j == k or abs(u[0] * o["direccion"][0] + u[1] * o["direccion"][1]) > 0.2:
                continue
            # la cumbrera del otro cuerpo, medida a lo largo de este (son perpendiculares)
            on = (-o["direccion"][1], o["direccion"][0])
            tr = o["cumbrera"] * (u[0] * on[0] + u[1] * on[1])
            ancho = c["franja"][1] - c["franja"][0]
            t0, t1 = c["tramo"]
            if t1 < tr <= t1 + ancho:
                c["tramo"][1] = round(tr, 3)
            elif t0 - ancho <= tr < t0:
                c["tramo"][0] = round(tr, 3)
    return {"pendientes_bordes": [datos[i][0] for i in bordes], "alturas_bordes": [datos[i][1] for i in bordes],
            "cuerpos_bordes": [datos[i][2] for i in bordes], "cuerpos": lista}
