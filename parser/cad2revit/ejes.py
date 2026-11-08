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


# ---------------------------------------------------------------- ediciones desde la app
MARGEN_EXTREMO = 0.6        # m que el eje sobresale del eje donde empieza o termina


def _cruce(a1, b1, a2, b2):
    """Intersección de las rectas (infinitas) a1-b1 y a2-b2, o None si son paralelas."""
    d1 = (b1[0] - a1[0], b1[1] - a1[1])
    d2 = (b2[0] - a2[0], b2[1] - a2[1])
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < 1e-9:
        return None
    t = ((a2[0] - a1[0]) * d2[1] - (a2[1] - a1[1]) * d2[0]) / den
    return (a1[0] + d1[0] * t, a1[1] + d1[1] * t)


def editar(ejes: list[dict], cambios: dict, advertencias: list[str]) -> list[dict]:
    """Aplica las ediciones de ejes hechas en la app (proyecto.yaml -> ejes_usuario), por nombre de eje:
         "1": null                                   # borrar
         "1": {posicion: 12.5}                       # mover (x de un eje vertical / y de uno horizontal)
         "1": {desde: "A", hasta: "D"}               # extender o recortar entre esos ejes
         "1": {nombre: "1'"}                         # renombrar
         "X": {inicio: [x, y], fin: [x, y]}          # eje nuevo (o reemplazar su trazado)
    """
    por = {e["nombre"]: dict(e) for e in ejes}
    renombres = {}
    for nombre, c in (cambios or {}).items():
        if c is None:
            if por.pop(nombre, None) is None:
                advertencias.append(f"Ejes: el eje '{nombre}' que se pidió borrar no existe")
            continue
        if c.get("inicio") and c.get("fin"):
            base = por.get(nombre, {"nombre": nombre})
            por[nombre] = dict(base, inicio=[float(v) for v in c["inicio"]], fin=[float(v) for v in c["fin"]],
                               origen="app" if nombre not in {e["nombre"] for e in ejes} else "editado en la app")
        e = por.get(nombre)
        if e is None:
            advertencias.append(f"Ejes: el eje '{nombre}' editado en la app ya no existe en el plano; se ignora")
            continue
        if c.get("posicion") is not None:
            (x1, y1), (x2, y2) = e["inicio"], e["fin"]
            v = float(c["posicion"])
            if abs(x2 - x1) < abs(y2 - y1):             # vertical: se mueve en X
                dx = v - (x1 + x2) / 2
                e["inicio"], e["fin"] = [x1 + dx, y1], [x2 + dx, y2]
            else:                                       # horizontal: se mueve en Y
                dy = v - (y1 + y2) / 2
                e["inicio"], e["fin"] = [x1, y1 + dy], [x2, y2 + dy]
            e["origen"] = "editado en la app"
        if c.get("nombre") and c["nombre"] != nombre:
            renombres[nombre] = c["nombre"]
    # extensión entre ejes: después de mover, con la posición final de todos
    for nombre, c in (cambios or {}).items():
        if not c or not (c.get("desde") or c.get("hasta")) or nombre not in por:
            continue
        e = por[nombre]
        a, b = tuple(e["inicio"]), tuple(e["fin"])
        L = math.dist(a, b) or 1.0
        u = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
        puntos = {}
        for clave in ("desde", "hasta"):
            o = por.get(c.get(clave)) if c.get(clave) else None
            if c.get(clave) and o is None:
                advertencias.append(f"Ejes: '{nombre}' {clave} '{c[clave]}': ese eje no existe")
                continue
            if o is not None:
                p = _cruce(a, b, tuple(o["inicio"]), tuple(o["fin"]))
                if p is None:
                    advertencias.append(f"Ejes: '{nombre}' y '{o['nombre']}' son paralelos; no se puede extender hasta él")
                else:
                    puntos[clave] = p
        t = lambda p: (p[0] - a[0]) * u[0] + (p[1] - a[1]) * u[1]      # noqa: E731
        t0, t1 = 0.0, L
        if "desde" in puntos and "hasta" in puntos:
            t0, t1 = sorted((t(puntos["desde"]), t(puntos["hasta"])))
            t0, t1 = t0 - MARGEN_EXTREMO, t1 + MARGEN_EXTREMO
        elif puntos:
            tp = t(next(iter(puntos.values())))
            # se reemplaza el extremo más cercano a ese eje
            if abs(tp - 0) <= abs(tp - L):
                t0 = tp - MARGEN_EXTREMO if tp <= L / 2 else tp + MARGEN_EXTREMO
            else:
                t1 = tp + MARGEN_EXTREMO if tp >= L / 2 else tp - MARGEN_EXTREMO
            t0, t1 = sorted((t0, t1))
        e["inicio"] = [round(a[0] + u[0] * t0, 4), round(a[1] + u[1] * t0, 4)]
        e["fin"] = [round(a[0] + u[0] * t1, 4), round(a[1] + u[1] * t1, 4)]
        e["origen"] = "editado en la app"
    for viejo, nuevo in renombres.items():
        if nuevo in por:
            advertencias.append(f"Ejes: no se puede renombrar '{viejo}' a '{nuevo}': ese nombre ya existe")
            continue
        e = por.pop(viejo)
        e["nombre"] = nuevo
        por[nuevo] = e
    return list(por.values())
