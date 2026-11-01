"""Aplicación de los lineamientos de estructuración: tipos de muro, muros
estructurales, columnas, vigas y fundaciones."""
from __future__ import annotations

import math
import re

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from .dxf_reader import Insercion, Punto
from .muros import Muro


# ---------------------------------------------------------------- tipos de muro
def asignar_tipo_muro(m: Muro, cfg, advertencias: list[str]) -> None:
    mat = cfg.materialidades
    # 1) Lego elegido para esta capa del CAD en el proyecto (tiene prioridad)
    elegido = ((cfg.proyecto.get("legos") or {}).get("muros") or {}).get(m.capa_cad)
    if elegido:
        m.tipo = elegido
        t = cfg.tipo_muro(m.tipo)
        if t and abs(t["espesor_total"] - m.espesor) > 0.03:
            advertencias.append(f"{m.id}: espesor dibujado {m.espesor} m distinto del lego '{m.tipo}' "
                                f"({t['espesor_total']} m); revisar")
        return
    # 2) Reglas automáticas de materialidades.yaml
    for r in mat.get("reglas_muro", []):
        if "capa_cad" in r and not re.search(r["capa_cad"], m.capa_cad):
            continue
        if "espesor_min" in r and m.espesor < r["espesor_min"] - 1e-6:
            continue
        if "espesor_max" in r and m.espesor > r["espesor_max"] + 1e-6:
            continue
        m.tipo = r["tipo"]
        break
    else:
        m.tipo = mat.get("tipo_muro_por_defecto")
        if m.tipo:
            advertencias.append(f"{m.id}: espesor {m.espesor} m / capa '{m.capa_cad}' sin regla; "
                                f"se usa '{m.tipo}'")
        else:
            m.tipo = f"Genérico {round(m.espesor * 100)} cm"
            advertencias.append(f"{m.id}: sin regla ni tipo por defecto; se crea '{m.tipo}'")
    t = cfg.tipo_muro(m.tipo)
    if t and abs(t["espesor_total"] - m.espesor) > 0.03:
        advertencias.append(f"{m.id}: espesor medido {m.espesor} m difiere del tipo '{m.tipo}' "
                            f"({t['espesor_total']} m); revisar")


def huella_muros(muros: list[Muro]):
    polys = [LineString([m.a, m.b]).buffer(m.espesor / 2, cap_style="square", join_style="mitre")
             for m in muros if m.largo > 0]
    return unary_union(polys) if polys else None


def contorno_exterior(muros: list[Muro]) -> Polygon | None:
    h = huella_muros(muros)
    if h is None or h.is_empty:
        return None
    geoms = list(h.geoms) if hasattr(h, "geoms") else [h]
    mayor = max(geoms, key=lambda g: Polygon(g.exterior).area)
    return Polygon(mayor.exterior)


def marcar_estructurales(muros: list[Muro], cfg) -> None:
    est = cfg.estructuracion.get("muros_estructurales") or {}
    emin = est.get("espesor_minimo")
    patron = re.compile(est["capas_cad"]) if est.get("capas_cad") else None
    contorno = contorno_exterior(muros) if est.get("perimetrales") else None
    anillo = contorno.exterior if contorno is not None else None
    for m in muros:
        t = cfg.tipo_muro(m.tipo) if m.tipo else None
        if anillo is not None:
            medio = LineString([m.a, m.b]).interpolate(0.5, normalized=True)
            m.perimetral = medio.distance(anillo) <= m.espesor / 2 + 0.03
        m.estructural = bool(
            (t and t.get("estructural"))
            or (emin is not None and m.espesor >= emin - 1e-6)
            or (patron and patron.search(m.capa_cad))
            or (est.get("perimetrales") and m.perimetral)
        )


# ---------------------------------------------------------------- columnas
def _rectangulo(puntos: list[Punto]) -> tuple[Punto, float, float, float]:
    """Centro, b, h, rotación (grados) del rectángulo mínimo que contiene los puntos."""
    r = Polygon(puntos).minimum_rotated_rectangle if len(puntos) >= 3 else None
    if r is None or r.is_empty or r.geom_type != "Polygon":
        x, y = zip(*puntos)
        return ((sum(x) / len(x), sum(y) / len(y)), 0.0, 0.0, 0.0)
    c = list(r.exterior.coords)[:4]
    l1, l2 = math.dist(c[0], c[1]), math.dist(c[1], c[2])
    ang = math.degrees(math.atan2(c[1][1] - c[0][1], c[1][0] - c[0][0])) % 180
    if ang >= 90 - 1e-6:
        ang -= 90
        l1, l2 = l2, l1
    cen = r.centroid
    return ((round(cen.x, 4), round(cen.y, 4)), round(l1, 3), round(l2, 3), round(ang, 2) + 0.0)


def detectar_columnas(inserciones: list[Insercion], polilineas: list[list[Punto]], cfg,
                      prefijo: str) -> list[dict]:
    c = cfg.estructuracion.get("columnas") or {}
    sec = c.get("seccion_defecto") or {"b": 0.3, "h": 0.3}
    out = []
    for ins in inserciones:
        if ins.esquinas and len(ins.esquinas) >= 3:
            centro, b, h, rot = _rectangulo(ins.esquinas)
        else:
            centro, b, h, rot = ins.punto, 0, 0, ins.rotacion
        if b < 0.05 or h < 0.05:
            b, h, centro = sec["b"], sec["h"], ins.punto
        out.append(dict(centro=centro, b=b, h=h, rotacion=rot))
    for poli in polilineas:
        centro, b, h, rot = _rectangulo(poli)
        out.append(dict(centro=centro, b=b, h=h, rotacion=rot))
    out.sort(key=lambda x: (x["centro"][1], x["centro"][0]))
    for k, col in enumerate(out, 1):
        col["id"] = f"{prefijo}-C{k:03d}"
        col["familia"] = c.get("familia_revit")
        col["material"] = c.get("material") or cfg.estructuracion.get("material_estructural")
        col["forma"] = c.get("forma", "rectangular")
        col["parametro_b"] = c.get("parametro_b", "b")
        col["parametro_h"] = c.get("parametro_h", "h")
    return out


# ---------------------------------------------------------------- vigas
def generar_vigas(columnas: list[dict], muros: list[Muro], cfg, prefijo: str) -> list[dict]:
    v = cfg.estructuracion.get("vigas") or {}
    sec = v.get("seccion_defecto") or {"b": 0.2, "h": 0.5}
    tol = v.get("tolerancia_alineacion", 0.10)
    luz = v.get("luz_maxima", 8.0)
    out = []
    if v.get("entre_columnas_alineadas"):
        for eje in (0, 1):            # 0: misma Y (viga horizontal), 1: misma X
            otro = 1 - eje
            grupos: list[list[dict]] = []
            for col in sorted(columnas, key=lambda c: c["centro"][otro]):
                for g in grupos:
                    if abs(g[0]["centro"][otro] - col["centro"][otro]) <= tol:
                        g.append(col)
                        break
                else:
                    grupos.append([col])
            for g in grupos:
                g.sort(key=lambda c: c["centro"][eje])
                for c1, c2 in zip(g, g[1:]):
                    if math.dist(c1["centro"], c2["centro"]) <= luz:
                        out.append(dict(inicio=c1["centro"], fin=c2["centro"], b=sec["b"], h=sec["h"],
                                        origen="entre_columnas"))
    if v.get("sobre_muros_estructurales"):
        for m in muros:
            if m.estructural:
                out.append(dict(inicio=m.a, fin=m.b, b=m.espesor, h=sec["h"], origen=f"sobre {m.id}"))
    for k, viga in enumerate(out, 1):
        viga["id"] = f"{prefijo}-B{k:03d}"
        viga["familia"] = v.get("familia_revit")
        viga["material"] = v.get("material") or cfg.estructuracion.get("material_estructural")
        viga["forma"] = v.get("forma", "rectangular")
        viga["parametro_b"] = v.get("parametro_b", "b")
        viga["parametro_h"] = v.get("parametro_h", "h")
    return out


# ---------------------------------------------------------------- fundaciones
def generar_fundaciones(muros: list[Muro], columnas: list[dict], cfg) -> dict:
    f = cfg.estructuracion.get("fundaciones") or {}
    res = {"corridas": [], "aisladas": []}
    if f.get("corridas_bajo_muros"):
        for m in muros:
            if m.estructural:
                res["corridas"].append(dict(muro=m.id, ancho=f.get("ancho_corrida", 0.6),
                                            alto=f.get("alto_corrida", 0.4)))
    if f.get("aisladas_bajo_columnas"):
        z = f.get("zapata_aislada") or {"b": 1.2, "l": 1.2, "h": 0.5}
        for c in columnas:
            res["aisladas"].append(dict(columna=c["id"], centro=c["centro"], b=z["b"], l=z["l"], h=z["h"],
                                        familia=f.get("familia_zapata_revit"),
                                        cota_sello=f.get("profundidad_sello", -1.2)))
    return res
