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
import re
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


def _pares_cercanos(segs: list[Segmento], angs: list[float], tol: float, emax: float, lmin: float):
    """Pares (i, j), i < j, de líneas casi paralelas y a menos de emax entre sí, sin comparar todas
    con todas: se agrupan por ángulo y se ordenan por desplazamiento (igual resultado, mucho más rápido)."""
    paso = max(tol, math.radians(0.5))
    nb = max(1, int(round(math.pi / paso)))
    cubetas: dict[int, list[int]] = {}
    for i, s in enumerate(segs):
        if s.largo >= lmin:
            cubetas.setdefault(int(angs[i] % math.pi / paso) % nb, []).append(i)
    vistos = set()
    for k in cubetas:
        # la cubeta y sus vecinas (una línea a 0,9° y otra a 1,1° caen en cubetas distintas)
        idx = sorted(set(cubetas[k]) | set(cubetas.get((k + 1) % nb, [])) | set(cubetas.get((k - 1) % nb, [])))
        ang = (k + 0.5) * paso
        n = (-math.sin(ang), math.cos(ang))
        u = (math.cos(ang), math.sin(ang))
        margen = emax + 1e-6
        items = []
        for i in idx:
            s = segs[i]
            ds = (s.a[0] * n[0] + s.a[1] * n[1], s.b[0] * n[0] + s.b[1] * n[1])
            items.append((min(ds), max(ds), i))
        items.sort()
        for a_i, (lo, hi, i) in enumerate(items):
            for lo2, hi2, j in items[a_i + 1:]:
                if lo2 > hi + margen:
                    break
                par = (i, j) if i < j else (j, i)
                if par not in vistos:
                    vistos.add(par)
    # orden estable igual al recorrido original (i creciente, luego j)
    return sorted(vistos)


def _candidatos(segs: list[Segmento], det: dict) -> list[_Candidato]:
    tol = math.radians(det["tolerancia_angulo_grados"])
    emin, emax, lmin = det["espesor_min"], det["espesor_max"], det["largo_min"]
    angs = [_angulo(s) for s in segs]
    out: list[_Candidato] = []
    for i, j in _pares_cercanos(segs, angs, tol, emax, lmin):
        si, sj = segs[i], segs[j]
        u = (math.cos(angs[i]), math.sin(angs[i]))
        if True:
            if _dif_angulo(angs[i], angs[j]) > tol:
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
    por_linea: dict[int, list[_Candidato]] = {}
    for c in out:
        por_linea.setdefault(c.i, []).append(c)
        por_linea.setdefault(c.j, []).append(c)
    keep = []
    for c in out:
        dominado = False
        for o in por_linea[c.i] + por_linea[c.j]:
            if o is c or o.espesor >= c.espesor - 1e-6:
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
        dirs = [m.direccion for m in muros]          # se recalcula en cada pasada (tras cada unión)
        for x in range(len(muros)):
            for y in range(x + 1, len(muros)):
                m, n = muros[x], muros[y]
                if abs(m.espesor - n.espesor) > 0.01:
                    continue
                u, v = dirs[x], dirs[y]
                if abs(u[0] * v[1] - u[1] * v[0]) > 0.01:
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
    # índice por celdas: cada extremo solo se compara con los muros cercanos (mismo resultado que
    # compararlo con todos, porque la unión exige que el punto quede a menos de ~0,5 m del otro muro)
    celda = 2.0
    margen = max((m.espesor for m in muros), default=0.4) * 2 + 1.0
    rejilla: dict[tuple[int, int], list[int]] = {}
    for k, n in enumerate(muros):
        x0, x1 = sorted((n.a[0], n.b[0]))
        y0, y1 = sorted((n.a[1], n.b[1]))
        for cx in range(int(math.floor((x0 - margen) / celda)), int(math.floor((x1 + margen) / celda)) + 1):
            for cy in range(int(math.floor((y0 - margen) / celda)), int(math.floor((y1 + margen) / celda)) + 1):
                rejilla.setdefault((cx, cy), []).append(k)
    for m in muros:
        for extremo in ("a", "b"):
            p = getattr(m, extremo)
            mejor, dmejor = None, None
            cercanos = rejilla.get((int(math.floor(p[0] / celda)), int(math.floor(p[1] / celda))), [])
            for n in (muros[k] for k in cercanos):
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


def largo_pareado_por_capa(segs: list[Segmento], det: dict) -> dict[str, tuple[float, float]]:
    """Por capa: (metros de líneas que tienen una paralela a distancia de espesor de muro, metros totales).
    Rápido (ordena por desplazamiento); sirve para sugerir qué capas tienen muros cuando sus nombres no lo dicen."""
    emin, emax, lmin = det["espesor_min"], det["espesor_max"], det["largo_min"]
    paso = math.radians(max(det.get("tolerancia_angulo_grados", 1.0), 0.5))
    por_capa: dict[str, list[Segmento]] = {}
    for s in segs:
        por_capa.setdefault(s.capa, []).append(s)
    out = {}
    for capa, lista in por_capa.items():
        total = sum(s.largo for s in lista)
        grupos: dict[int, list] = {}
        for s in lista:
            if s.largo < lmin:
                continue
            ang = _angulo(s) % math.pi
            k = int(round(ang / paso)) % int(round(math.pi / paso))
            u = (math.cos(k * paso), math.sin(k * paso))
            n = (-u[1], u[0])
            d = s.a[0] * n[0] + s.a[1] * n[1]
            t = sorted((s.a[0] * u[0] + s.a[1] * u[1], s.b[0] * u[0] + s.b[1] * u[1]))
            grupos.setdefault(k, []).append([d, t[0], t[1], 0.0, 0.0])     # d, t0, t1, solape abajo, solape arriba
        for items in grupos.values():
            items.sort()
            for i, a in enumerate(items):
                for b in items[i + 1:i + 200]:
                    sep = b[0] - a[0]
                    if sep > emax:
                        break
                    if sep < emin:
                        continue
                    sol = min(a[2], b[2]) - max(a[1], b[1])
                    if sol >= lmin:
                        a[4] += sol          # b está "arriba" de a
                        b[3] += sol
        # cara de muro: paralela por UN lado (la otra cara). Con paralelas a ambos lados es achurado.
        def cara(x):
            largo = x[2] - x[1]
            if min(x[3], x[4]) >= 0.5 * largo:
                return 0.0
            return min(max(x[3], x[4]), largo)
        pareado = sum(cara(x) for it in grupos.values() for x in it)
        out[capa] = (round(pareado, 1), round(total, 1))
    return out


NO_MURO = re.compile(r"(?i)mobil|mueble|equip|sanit|artefact|reguero|zanja|cota|dim|texto|nota|achur|hatch|mobiliario")


def sugerir_capas_muro(segs: list[Segmento], det: dict) -> list[tuple[str, float]]:
    """Capas que por su dibujo parecen muros (caras paralelas a espesor de muro), sin mobiliario ni
    instalaciones. Se quedan las que tienen al menos el 15 % de los metros de la mejor."""
    cand = []
    for capa, (p, t) in largo_pareado_por_capa(segs, det).items():
        m = p / 2
        if m >= 5 and p >= 0.25 * t and not NO_MURO.search(capa):
            cand.append((capa, round(m, 1)))
    if not cand:
        return []
    mejor = max(m for _, m in cand)
    return sorted([c for c in cand if c[1] >= 0.15 * mejor], key=lambda c: -c[1])


def quitar_encimados(muros: list[Muro]) -> tuple[list[Muro], list[Muro]]:
    """Muros contenidos en otro más grueso (paralelos, dentro de su espesor y superpuestos en >= 80 % de
    su largo): son líneas de vidrio, marcos o muebles dibujados dentro del muro. Revit los superpone
    ("walls overlap") y aloja las ventanas en ellos en vez del muro real. Devuelve (quedan, quitados)."""
    quedan: list[Muro] = []
    for m in sorted(muros, key=lambda x: (-x.espesor, -x.largo)):
        u = m.direccion
        contenido = False
        for n in quedan:
            v = n.direccion
            if abs(u[0] * v[1] - u[1] * v[0]) > 0.05:
                continue
            banda = n.espesor / 2 + 0.03
            if dist_perp(m.a, n.a, v) > banda or dist_perp(m.b, n.a, v) > banda:
                continue
            t = sorted((proyectar(m.a, n.a, v), proyectar(m.b, n.a, v)))
            if min(t[1], n.largo) - max(t[0], 0.0) >= 0.8 * m.largo:
                contenido = True
                break
        if not contenido:
            quedan.append(m)
    ids = {id(m) for m in quedan}
    return [m for m in muros if id(m) in ids], [m for m in muros if id(m) not in ids]
