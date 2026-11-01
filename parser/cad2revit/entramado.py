"""Entramado de muros/tabiques de madera: pies derechos, soleras, cadenetas,
dinteles y alféizares, calculados a partir del eje del muro, sus aberturas y
la configuración "entramado" del tipo de muro (materialidades.yaml).

Cada pieza se exporta como un prisma recto:
    inicio, fin   puntos 3D (m) del eje de la pieza
    ancho         lado de la sección contenido en el plano del muro
    profundidad   lado de la sección perpendicular al muro (espesor)
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .muros import Muro, dist_punto_segmento, proyectar

TOL = 1e-6


@dataclass
class _Vertical:
    t: float          # posición a lo largo del muro (centro de la pieza)
    z0: float
    z1: float
    rol: str


def _recortes_extremos(m: Muro, otros: list[Muro]) -> tuple[float, float]:
    """Cuánto recortar en cada extremo para que el entramado termine en la cara
    del muro con el que se une (los ejes se prolongan hasta el eje del vecino)."""
    rec = []
    for p in (m.a, m.b):
        r = 0.0
        for o in otros:
            if o is m:
                continue
            if dist_punto_segmento(p, o.a, o.b) <= o.espesor / 2 + 0.02:
                # el extremo cae dentro del muro vecino
                r = max(r, o.espesor / 2)
        rec.append(r)
    return rec[0], rec[1]


def generar(muro: Muro, otros: list[Muro], conf: dict, z_base: float, z_tope: float,
            aberturas: list[dict]) -> dict:
    sec = conf.get("pie_derecho") or {"b": 0.050, "h": 0.076}
    b, h = float(sec["b"]), float(sec["h"])
    sep_v = float(conf.get("separacion_pies_derechos", 0.40))
    sep_h = float(conf.get("separacion_horizontales", 0.40))
    con_soleras = conf.get("soleras", True)
    con_refuerzo = conf.get("refuerzo_vanos", True)

    u = muro.direccion
    r0, r1 = _recortes_extremos(muro, otros)
    s0, s1 = r0, muro.largo - r1
    if s1 - s0 < 2 * b:
        return {"muro": muro.id, "piezas": []}

    def punto(t: float, z: float) -> list[float]:
        return [round(muro.a[0] + u[0] * t, 4), round(muro.a[1] + u[1] * t, 4), round(z, 4)]

    piezas: list[dict] = []

    def vertical(t, z0, z1, rol):
        if z1 - z0 > 0.02:
            piezas.append({"rol": rol, "inicio": punto(t, z0), "fin": punto(t, z1), "ancho": b, "profundidad": h})

    def horizontal(t0, t1, zc, rol):
        if t1 - t0 > 0.02:
            piezas.append({"rol": rol, "inicio": punto(t0, zc), "fin": punto(t1, zc), "ancho": b, "profundidad": h})

    # Vanos en coordenadas del muro: (t0, t1, z_inferior, z_superior, es_puerta)
    vanos = []
    for a in aberturas:
        tc = proyectar(tuple(a["centro"]), muro.a, u)
        t0, t1 = max(s0 + b, tc - a["ancho"] / 2), min(s1 - b, tc + a["ancho"] / 2)
        if t1 - t0 < 0.05:
            continue
        zi = z_base + a["antepecho"]
        zs = min(z_tope - (b if con_soleras else 0) - b, zi + a["alto"])
        vanos.append((t0, t1, zi, zs, a["antepecho"] < 0.01))

    zb = z_base + (b if con_soleras else 0)      # cara superior de la solera inferior
    zt = z_tope - (b if con_soleras else 0)      # cara inferior de la solera superior

    # Soleras (la inferior se interrumpe en las puertas)
    if con_soleras:
        cortes = sorted((v[0], v[1]) for v in vanos if v[4])
        t = s0
        for c0, c1 in cortes:
            horizontal(t, c0, z_base + b / 2, "solera_inferior")
            t = c1
        horizontal(t, s1, z_base + b / 2, "solera_inferior")
        horizontal(s0, s1, z_tope - b / 2, "solera_superior")

    # Pies derechos
    verticales: list[_Vertical] = [_Vertical(s0 + b / 2, zb, zt, "pie_derecho"),
                                   _Vertical(s1 - b / 2, zb, zt, "pie_derecho")]
    t = s0 + sep_v
    while t < s1 - b / 2 - b:
        verticales.append(_Vertical(t, zb, zt, "pie_derecho"))
        t += sep_v

    if con_refuerzo:
        for t0, t1, zi, zs, puerta in vanos:
            jambas = (t0 - b / 2, t1 + b / 2)
            nuevos = []
            for v in verticales:
                if any(abs(v.t - j) < b - TOL for j in jambas):
                    continue                      # lo reemplaza la jamba
                if t0 - TOL < v.t < t1 + TOL:
                    # pie derecho cortado por el vano: trozos bajo el alféizar y sobre el dintel
                    if not puerta:
                        nuevos.append(_Vertical(v.t, zb, zi - b, "pie_derecho_corto"))
                    nuevos.append(_Vertical(v.t, zs + b, zt, "pie_derecho_corto"))
                    continue
                nuevos.append(v)
            nuevos += [_Vertical(j, zb, zt, "jamba") for j in jambas]
            verticales = nuevos
            horizontal(t0, t1, zs + b / 2, "dintel")
            if not puerta:
                horizontal(t0, t1, zi - b / 2, "alfeizar")

    verticales = [v for v in verticales if v.z1 - v.z0 > 0.02]
    for v in sorted(verticales, key=lambda v: v.t):
        vertical(v.t, v.z0, v.z1, v.rol)

    # Cadenetas (horizontales entre pies derechos)
    zc = z_base + sep_h
    while zc + b / 2 < zt - TOL:
        if zc - b / 2 < zb - TOL:
            zc += sep_h
            continue
        cruzan = sorted((v for v in verticales if v.z0 < zc - b / 2 + TOL and v.z1 > zc + b / 2 - TOL),
                        key=lambda v: v.t)
        for v1, v2 in zip(cruzan, cruzan[1:]):
            g0, g1 = v1.t + b / 2, v2.t - b / 2
            if g1 - g0 < 0.02:
                continue
            gm = (g0 + g1) / 2
            dentro = any(t0 - b <= gm <= t1 + b and zi - 2 * b <= zc <= zs + 2 * b
                         for t0, t1, zi, zs, _ in vanos)
            if dentro:
                continue
            horizontal(g0, g1, zc, "cadeneta")
        zc += sep_h

    resumen: dict[str, int] = {}
    for p in piezas:
        resumen[p["rol"]] = resumen.get(p["rol"], 0) + 1
    largo_total = sum(math.dist(p["inicio"], p["fin"]) for p in piezas)
    return {
        "muro": muro.id, "material": conf.get("material"), "forma": "rectangular", "direccion": [round(u[0], 6), round(u[1], 6)],
        "seccion": {"b": b, "h": h}, "piezas": piezas, "resumen": resumen,
        "revit": conf.get("revit") or {},
        "metros_lineales": round(largo_total, 2),
    }
