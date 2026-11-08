"""Vistas dentro de un mismo plano.

Es común que un DWG traiga la planta, las fachadas y los cortes dibujados lado a lado en el mismo
espacio modelo. Aquí se separan en "vistas" (grupos de dibujo separados por espacio vacío) y se
sugiere cuál es la planta y cuáles son elevaciones. El usuario confirma en la app.

Una vista se identifica por su caja [x0, y0, x1, y1] en metros, que se guarda en proyecto.yaml.
"""
from __future__ import annotations

import math
import re

from .dxf_reader import DibujoCAD
from .muros import largo_pareado_por_capa

SEPARACION = 3.0          # m de vacío que separan dos vistas
LARGO_IGNORADO = 80.0     # líneas más largas (marcos de lámina, calles) no unen vistas
MAX_VISTAS = 12


def _celda(x: float, y: float, g: float) -> tuple[int, int]:
    return (math.floor(x / g), math.floor(y / g))


def detectar(dib: DibujoCAD, es_abertura=None, patron_nivel: str | None = None,
             patron_muros: re.Pattern | None = None, det: dict | None = None) -> list[dict]:
    """Lista de vistas, de mayor a menor. Si el plano tiene una sola vista, devuelve una sola."""
    g = SEPARACION
    segs = [s for s in dib.segmentos if math.dist(s.a, s.b) <= LARGO_IGNORADO]
    if not segs:
        return []
    padre: dict[tuple[int, int], tuple[int, int]] = {}

    def raiz(c):
        while padre[c] != c:
            padre[c] = padre[padre[c]]
            c = padre[c]
        return c

    def unir(a, b):
        ra, rb = raiz(a), raiz(b)
        if ra != rb:
            padre[rb] = ra

    celda_seg = []
    for s in segs:
        n = min(2000, max(1, int(math.dist(s.a, s.b) / (g / 2))))
        celdas = {_celda(s.a[0] + (s.b[0] - s.a[0]) * k / n, s.a[1] + (s.b[1] - s.a[1]) * k / n, g) for k in range(n + 1)}
        for c in celdas:
            padre.setdefault(c, c)
        primera = next(iter(celdas))
        for c in celdas:
            unir(primera, c)
        celda_seg.append(primera)
    for (cx, cy) in list(padre):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                v = (cx + dx, cy + dy)
                if v in padre:
                    unir((cx, cy), v)

    grupos: dict[tuple[int, int], dict] = {}
    for s, c in zip(segs, celda_seg):
        r = raiz(c)
        gr = grupos.setdefault(r, {"segs": [], "ins": [], "txt": []})
        gr["segs"].append(s)

    def grupo_de(p):
        c = _celda(p[0], p[1], g)
        for dx in (0, -1, 1):
            for dy in (0, -1, 1):
                v = (c[0] + dx, c[1] + dy)
                if v in padre:
                    return grupos.get(raiz(v))
        return None

    for i in dib.inserciones:
        gr = grupo_de(i.punto)
        if gr is not None:
            gr["ins"].append(i)
    for t in dib.textos:
        gr = grupo_de(t.punto)
        if gr is not None:
            gr["txt"].append(t)

    det = det or {"espesor_min": 0.05, "espesor_max": 0.40, "largo_min": 0.20}
    vistas = []
    for gr in grupos.values():
        total = sum(math.dist(s.a, s.b) for s in gr["segs"])
        textos_nivel = sum(1 for t in gr["txt"] if patron_nivel and re.search(patron_nivel, t.texto))
        if (len(gr["segs"]) < 8 or total < 5) and not textos_nivel:
            continue                                  # rótulos, flechas de norte, textos sueltos
        xs = [p[0] for s in gr["segs"] for p in (s.a, s.b)]
        ys = [p[1] for s in gr["segs"] for p in (s.a, s.b)]
        caja = [round(float(min(xs)), 2), round(float(min(ys)), 2), round(float(max(xs)), 2), round(float(max(ys)), 2)]
        de_muros = [s for s in gr["segs"] if patron_muros and patron_muros.search(s.capa)] or gr["segs"]
        muros_m = sum(p for p, _ in largo_pareado_por_capa(de_muros, det).values()) / 2
        aberturas = sum(1 for i in gr["ins"] if es_abertura and es_abertura(i))
        vistas.append({"caja": caja, "ancho": round(caja[2] - caja[0], 1), "alto": round(caja[3] - caja[1], 1),
                       "lineas": len(gr["segs"]), "muros_m": round(float(muros_m), 1), "aberturas": aberturas,
                       "textos_nivel": textos_nivel})
    vistas.sort(key=lambda v: -(v["ancho"] * v["alto"] + v["lineas"]))
    vistas = vistas[:MAX_VISTAS]
    _sugerir(vistas)
    for k, v in enumerate(vistas):
        v["k"] = k
    return vistas


def _sugerir(vistas: list[dict]) -> None:
    """Planta: la que tiene más puertas/ventanas (y muros); elevación: las que tienen cotas de nivel."""
    if not vistas:
        return
    puntaje = [v["aberturas"] * 10 + v["muros_m"] for v in vistas]
    mejor = max(puntaje)
    for v, p in zip(vistas, puntaje):
        if p == mejor or (v["aberturas"] and p >= 0.5 * mejor):
            v["sugerido"] = "planta"
        elif v["textos_nivel"]:
            v["sugerido"] = "elevacion"
        else:
            v["sugerido"] = "no"


def recortar(dib: DibujoCAD, cajas: list[list[float]], margen: float = 0.5) -> DibujoCAD:
    """Solo lo que cae dentro de alguna de las cajas (vistas elegidas del plano)."""
    if not cajas:
        return dib

    def dentro(p):
        return any(c[0] - margen <= p[0] <= c[2] + margen and c[1] - margen <= p[1] <= c[3] + margen for c in cajas)

    return DibujoCAD(
        archivo=dib.archivo, factor=dib.factor,
        segmentos=[s for s in dib.segmentos if dentro(((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2))],
        polilineas_cerradas=[(p, capa) for p, capa in dib.polilineas_cerradas if p and dentro(p[0])],
        inserciones=[i for i in dib.inserciones if dentro(i.punto)],
        textos=[t for t in dib.textos if dentro(t.punto)],
    )


def archivo_de(entrada) -> str:
    """Las elevaciones pueden ser una ruta o {archivo, zona}."""
    return entrada["archivo"] if isinstance(entrada, dict) else entrada


def zonas_de(entrada) -> list[list[float]]:
    if not isinstance(entrada, dict):
        return []
    if entrada.get("zonas"):
        return entrada["zonas"]
    return [entrada["zona"]] if entrada.get("zona") else []
