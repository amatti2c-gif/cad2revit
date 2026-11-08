"""Ejes del proyecto: desde la capa de ejes del CAD o, si no hay, generados desde los muros.

En los DWG reales un mismo eje suele venir partido en varios trozos o repetido; aquí se
unen los segmentos colineales en una sola línea por eje.
"""
from __future__ import annotations

import math
import string

TOL_ANG = math.radians(2.0)


def _letras(n: int) -> list[str]:
    letras = [c for c in string.ascii_uppercase if c not in "IO"]
    return [letras[i % len(letras)] * (i // len(letras) + 1) for i in range(n)]


def _agrupar(items: list[tuple[float, float, float, float]], tol_d: float) -> list[list]:
    """items: (angulo, desplazamiento, t0, t1, peso). Agrupa por ángulo y desplazamiento."""
    grupos: list[list] = []
    for it in sorted(items, key=lambda x: (round(x[0], 3), x[1])):
        for g in grupos:
            ang = g[0][0]
            dif = abs(it[0] - ang) % math.pi
            if min(dif, math.pi - dif) <= TOL_ANG and abs(it[1] - g[0][1]) <= tol_d:
                g.append(it)
                break
        else:
            grupos.append([it])
    return grupos


def _lineas(grupos: list[list], margen: float, largo_min: float) -> list[tuple]:
    out = []
    for g in grupos:
        peso = sum(x[4] for x in g)
        if peso < largo_min:
            continue
        ang = g[0][0]
        d = sum(x[1] * x[4] for x in g) / peso
        t0, t1 = min(x[2] for x in g) - margen, max(x[3] for x in g) + margen
        u = (math.cos(ang), math.sin(ang))
        n = (-u[1], u[0])
        a = (n[0] * d + u[0] * t0, n[1] * d + u[1] * t0)
        b = (n[0] * d + u[0] * t1, n[1] * d + u[1] * t1)
        out.append((ang, d, (round(a[0], 4), round(a[1], 4)), (round(b[0], 4), round(b[1], 4))))
    return out


def _item(a, b, peso=None):
    ang = math.atan2(b[1] - a[1], b[0] - a[0]) % math.pi
    u = (math.cos(ang), math.sin(ang))
    n = (-u[1], u[0])
    d = a[0] * n[0] + a[1] * n[1]
    t = sorted((a[0] * u[0] + a[1] * u[1], b[0] * u[0] + b[1] * u[1]))
    return (ang, d, t[0], t[1], peso if peso is not None else math.dist(a, b))


def desde_cad(segmentos, largo_min: float = 1.0) -> list[tuple]:
    """Une los segmentos colineales de la capa de ejes (tolerancia 5 cm)."""
    items = [_item(s.a, s.b) for s in segmentos if math.dist(s.a, s.b) >= 0.2]
    return _lineas(_agrupar(items, 0.05), 0.0, largo_min)


def desde_muros(muros, largo_min: float = 1.5) -> list[tuple]:
    """Ejes por las líneas de muros alineados (tolerancia 20 cm), con 1 m de margen."""
    items = [_item(m.a, m.b) for m in muros if m.largo >= 0.3]
    return _lineas(_agrupar(items, 0.20), 1.0, largo_min)


def nombrar(lineas: list[tuple], ecfg: dict, origen: str) -> list[dict]:
    """Verticales con números (ordenadas por X), horizontales con letras (por Y), otras D1, D2..."""
    vert, hori, otras = [], [], []
    for ang, d, a, b in lineas:
        dx, dy = abs(math.cos(ang)), abs(math.sin(ang))
        (vert if dy > 0.97 else hori if dx > 0.97 else otras).append((a, b))
    vert.sort(key=lambda l: (l[0][0] + l[1][0]) / 2)
    hori.sort(key=lambda l: (l[0][1] + l[1][1]) / 2)
    def estilo(clave, defecto):
        return ecfg.get(clave, defecto)
    nombres_v = [str(i + 1) for i in range(len(vert))] if estilo("nomenclatura_x", "numeros") == "numeros" else _letras(len(vert))
    nombres_h = _letras(len(hori)) if estilo("nomenclatura_y", "letras") == "letras" else [str(i + 1) for i in range(len(hori))]
    out = [dict(nombre=n, inicio=list(a), fin=list(b), origen=origen) for n, (a, b) in zip(nombres_v, vert)]
    out += [dict(nombre=n, inicio=list(a), fin=list(b), origen=origen) for n, (a, b) in zip(nombres_h, hori)]
    out += [dict(nombre=f"D{i + 1}", inicio=list(a), fin=list(b), origen=origen) for i, (a, b) in enumerate(otras)]
    return out
