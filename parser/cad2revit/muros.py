"""Detección de muros a partir de pares de líneas paralelas en planta.

Algoritmo:
 1. Para cada par de segmentos paralelos de capas de muro, cuya distancia está
    en [espesor_min, espesor_max] y que se solapan a lo largo, se crea un
    candidato: eje = línea media sobre el tramo solapado, espesor = distancia.
 2. Se descartan candidatos "dominados" (cuando una cara tiene otra cara más
    cercana que se solapa en el mismo tramo).
 3. Se unen tramos colineales del mismo espesor separados por uniones en T o
    por vanos que contienen una puerta/ventana.
 4. Se extienden los extremos hasta la intersección con muros vecinos
    (esquinas en L y uniones en T).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .dxf_reader import Punto, Segmento


@dataclass
class Muro:
    id: str
    a: Punto
    b: Punto
    espesor: float
    capa_cad: str
    tipo: str | None = None
    estructural: bool = False
    perimetral: bool = False
    origen: list[int] = field(default_factory=list)

    @property
    def largo(self) -> float:
        return math.dist(self.a, self.b)

    @property
    def direccion(self) -> Punto:
        L = self.largo or 1.0
        return ((self.b[0] - self.a[0]) / L, (self.b[1] - self.a[1]) / L)


# ---------------------------------------------------------------- utilidades
def _angulo(s: Segmento) -> float:
    ang = math.atan2(s.b[1] - s.a[1], s.b[0] - s.a[0]) % math.pi
    return ang


def _dif_angulo(a1: float, a2: float) -> float:
    d = abs(a1 - a2) % math.pi
    return min(d, math.pi - d)


def proyectar(p: Punto, origen: Punto, u: Punto) -> float:
    return (p[0] - origen[0]) * u[0] + (p[1] - origen[1]) * u[1]


def dist_perp(p: Punto, origen: Punto, u: Punto) -> float:
    return abs((p[0] - origen[0]) * u[1] - (p[1] - origen[1]) * u[0])


def dist_punto_segmento(p: Punto, a: Punto, b: Punto) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
    return math.dist(p, (a[0] + t * dx, a[1] + t * dy))


def _interseccion(a1: Punto, u1: Punto, a2: Punto, u2: Punto) -> Punto | None:
    den = u1[0] * u2[1] - u1[1] * u2[0]
    if abs(den) < 1e-9:
        return None
    t = ((a2[0] - a1[0]) * u2[1] - (a2[1] - a1[1]) * u2[0]) / den
    return (a1[0] + t * u1[0], a1[1] + t * u1[1])


def _r(p: Punto) -> Punto:
    return (round(p[0], 4), round(p[1], 4))


def _canonico(a: Punto, b: Punto) -> tuple[Punto, Punto]:
    """Orienta el eje siempre de izquierda a derecha / abajo hacia arriba."""
    return (a, b) if (a[0], a[1]) <= (b[0], b[1]) else (b, a)


# ---------------------------------------------------------------- paso 1-2
@dataclass
class _Candidato:
    i: int
    j: int
    a: Punto
    b: Punto
    t0: float
    t1: float
    espesor: float
    capa: str
    origen: Punto
    u: Punto


def _candidatos(segs: list[Segmento], det: dict) -> list[_Candidato]:
    tol = math.radians(det["tolerancia_angulo_grados"])
    emin, emax, lmin = det["espesor_min"], det["espesor_max"], det["largo_min"]
    angs = [_angulo(s) for s in segs]
    out: list[_Candidato] = []
    for i, si in enumerate(segs):
        if si.largo < lmin:
            continue
        u = (math.cos(angs[i]), math.sin(angs[i]))
        for j in range(i + 1, len(segs)):
            sj = segs[j]
            if sj.largo < lmin or _dif_angulo(angs[i], angs[j]) > tol:
                continue
            d = (dist_perp(sj.a, si.a, u) + dist_perp(sj.b, si.a, u)) / 2
            if not (emin - 1e-6 <= d <= emax + 1e-6):
                continue
            ti = sorted((proyectar(si.a, si.a, u), proyectar(si.b, si.a, u)))
            tj = sorted((proyectar(sj.a, si.a, u), proyectar(sj.b, si.a, u)))
            t0, t1 = max(ti[0], tj[0]), min(ti[1], tj[1])
            if t1 - t0 < lmin:
                continue
            # normal hacia sj
            n = (-u[1], u[0])
            lado = (sj.a[0] - si.a[0]) * n[0] + (sj.a[1] - si.a[1]) * n[1]
            off = lado / 2
            p0 = (si.a[0] + u[0] * t0 + n[0] * off, si.a[1] + u[1] * t0 + n[1] * off)
            p1 = (si.a[0] + u[0] * t1 + n[0] * off, si.a[1] + u[1] * t1 + n[1] * off)
            out.append(_Candidato(i, j, p0, p1, t0, t1, round(d, 4), si.capa, si.a, u))
    # descartar dominados: misma cara con otra cara más cercana en el mismo tramo
    keep = []
    for c in out:
        dominado = False
        for o in out:
            if o is c or o.espesor >= c.espesor - 1e-6:
                continue
            if not ({o.i, o.j} & {c.i, c.j}):
                continue
            to = sorted((proyectar(o.a, c.origen, c.u), proyectar(o.b, c.origen, c.u)))
            sol = min(to[1], c.t1) - max(to[0], c.t0)
            if sol > 0.5 * (c.t1 - c.t0):
                dominado = True
                break
        if not dominado:
            keep.append(c)
    return keep


# ---------------------------------------------------------------- paso 3
def _unir_colineales(muros: list[Muro], puntos_vano: list[Punto], det: dict) -> list[Muro]:
    """Une tramos colineales del mismo espesor (uniones en T o vanos con abertura)."""
    vano_max = det["vano_max"]
    emax = det["espesor_max"]
    cambiado = True
    while cambiado:
        cambiado = False
        for x in range(len(muros)):
            for y in range(x + 1, len(muros)):
                m, n = muros[x], muros[y]
                if abs(m.espesor - n.espesor) > 0.01:
                    continue
                u = m.direccion
                if abs(u[0] * n.direccion[1] - u[1] * n.direccion[0]) > 0.01:
                    continue
                if dist_perp(n.a, m.a, u) > 0.01 or dist_perp(n.b, m.a, u) > 0.01:
                    continue
                tm = sorted((0.0, m.largo))
                tn = sorted((proyectar(n.a, m.a, u), proyectar(n.b, m.a, u)))
                gap = max(tn[0] - tm[1], tm[0] - tn[1])
                if gap > vano_max:
                    continue
                if gap > emax + 1e-6:
                    # sólo unir si hay una abertura dentro del vano
                    g0, g1 = (tm[1], tn[0]) if tn[0] >= tm[1] else (tn[1], tm[0])
                    hay = any(g0 - 0.05 <= proyectar(p, m.a, u) <= g1 + 0.05
                              and dist_perp(p, m.a, u) <= m.espesor / 2 + 0.6
                              for p in puntos_vano)
                    if not hay:
                        continue
                t0, t1 = min(tm[0], tn[0]), max(tm[1], tn[1])
                a = (m.a[0] + u[0] * t0, m.a[1] + u[1] * t0)
                b = (m.a[0] + u[0] * t1, m.a[1] + u[1] * t1)
                m.a, m.b = _canonico(_r(a), _r(b))
                m.origen += n.origen
                muros.pop(y)
                cambiado = True
                break
            if cambiado:
                break
    return muros


# ---------------------------------------------------------------- paso 4
def _resolver_uniones(muros: list[Muro]) -> None:
    for m in muros:
        for extremo in ("a", "b"):
            p = getattr(m, extremo)
            mejor, dmejor = None, None
            for n in muros:
                if n is m:
                    continue
                q = _interseccion(m.a, m.direccion, n.a, n.direccion)
                if q is None:
                    continue
                alcance = max(m.espesor, n.espesor) + 0.02
                d = math.dist(p, q)
                if d > alcance:
                    continue
                # q debe caer sobre n (extendido por su espesor)
                tq = proyectar(q, n.a, n.direccion)
                if not (-n.espesor - 0.02 <= tq <= n.largo + n.espesor + 0.02):
                    continue
                # no acortar el muro más allá de la mitad
                tm = proyectar(q, m.a, m.direccion)
                if not (-alcance <= tm <= m.largo + alcance):
                    continue
                if dmejor is None or d < dmejor:
                    mejor, dmejor = q, d
            if mejor is not None:
                setattr(m, extremo, _r(mejor))
    for m in muros:
        m.a, m.b = _canonico(m.a, m.b)


def detectar_muros(segs: list[Segmento], det: dict, puntos_vano: list[Punto], prefijo: str) -> list[Muro]:
    cands = _candidatos(segs, det)
    muros = []
    vistos = set()
    for c in cands:
        a, b = _canonico(_r(c.a), _r(c.b))
        clave = (a, b, c.espesor)
        if clave in vistos:
            continue
        vistos.add(clave)
        muros.append(Muro(id="", a=a, b=b, espesor=c.espesor, capa_cad=c.capa, origen=[c.i, c.j]))
    muros = _unir_colineales(muros, puntos_vano, det)
    _resolver_uniones(muros)
    muros = [m for m in muros if m.largo >= det["largo_min"]]
    muros.sort(key=lambda m: (m.a[1], m.a[0], m.b[1], m.b[0]))
    for k, m in enumerate(muros, 1):
        m.id = f"{prefijo}-M{k:03d}"
    return muros
