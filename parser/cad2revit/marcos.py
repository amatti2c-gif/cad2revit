"""Marcos de acero: de momento y arriostrados en cruz.

En planta, cada marco se dibuja como una línea (su eje) en una capa de marcos.
Por cada línea y cada piso se generan:
  - columnas en los extremos (y en divisiones intermedias si el marco supera
    el vano máximo del lego),
  - una viga por vano en el nivel superior,
  - en marcos arriostrados, dos diagonales en X por vano.
Las columnas compartidas por dos marcos que se tocan se crean una sola vez.
"""
from __future__ import annotations

import math
import re

from .dxf_reader import Segmento

SISTEMAS = {"momento", "arriostrado_x"}
FORMAS = {"W", "C", "L", "tubo", "rectangular", "circular"}


def tipo_para_capa(capa: str, cfg) -> str | None:
    """Lego de marco para una capa del CAD: el elegido en el proyecto o, si no, las reglas."""
    elegido = ((cfg.proyecto.get("legos") or {}).get("marcos") or {}).get(capa)
    if elegido:
        return elegido
    for r in cfg.materialidades.get("reglas_marco", []):
        if re.search(r.get("capa_cad", ""), capa):
            return r.get("tipo")
    return cfg.materialidades.get("tipo_marco_por_defecto")


def _perfil(p: dict, material: str, rol: str) -> dict:
    return {"rol": rol, "material": material, "forma": p.get("forma", "W"),
            "perfil": p.get("perfil") or None, "familia": p.get("familia") or None,
            "b": float(p.get("b", 0.15)), "h": float(p.get("h", 0.2))}


def generar(segmentos: list, cfg, nivel: str, sup: str | None, cota: dict[str, float],
            prefijo: str, advertencias: list[str], columnas_vistas: set | None = None,
            numero_inicial: int = 1) -> list[dict]:
    """segmentos: Segmento del CAD, o tuplas (Segmento, lego, origen) para marcos que no vienen
    de una capa (p. ej. los propuestos por tramo de eje)."""
    items = [x if isinstance(x, tuple) else (x, None, None) for x in segmentos]
    if sup is None:
        if items:
            advertencias.append(f"{nivel}: hay marcos pero no existe un nivel superior; se omiten")
        return []
    z0, z1 = cota[nivel], cota[sup]
    out = []
    if columnas_vistas is None:
        columnas_vistas = set()
    for k, (s, lego_fijo, origen) in enumerate(sorted(items, key=lambda x: (x[0].a, x[0].b)), numero_inicial):
        nombre = lego_fijo or tipo_para_capa(s.capa, cfg)
        lego = next((t for t in cfg.materialidades.get("tipos_marco", []) if t["nombre"] == nombre), None)
        if lego is None:
            advertencias.append(f"Marco en capa '{s.capa}': no hay lego de marco asignado; se omite")
            continue
        largo = s.largo
        if largo < 0.3:
            continue
        vano_max = float(lego.get("vano_maximo") or 0)
        n = max(1, math.ceil(largo / vano_max - 1e-9)) if vano_max > 0 else 1
        pts = [(round(s.a[0] + (s.b[0] - s.a[0]) * i / n, 4), round(s.a[1] + (s.b[1] - s.a[1]) * i / n, 4))
               for i in range(n + 1)]
        mat = lego["material"]
        cols = []
        for p in pts:
            clave = (round(p[0], 3), round(p[1], 3))
            if clave in columnas_vistas:
                continue                      # columna ya creada por otro marco del mismo piso
            columnas_vistas.add(clave)
            cols.append({"punto": list(p), "z0": z0, "z1": z1})
        vigas = [{"inicio": [a[0], a[1], z1], "fin": [b[0], b[1], z1]} for a, b in zip(pts, pts[1:])]
        diagonales = []
        if lego["sistema"] == "arriostrado_x":
            for a, b in zip(pts, pts[1:]):
                diagonales.append({"inicio": [a[0], a[1], z0], "fin": [b[0], b[1], z1]})
                diagonales.append({"inicio": [b[0], b[1], z0], "fin": [a[0], a[1], z1]})
        out.append({
            "id": f"{prefijo}-F{k:03d}", "nivel": nivel, "nivel_superior": sup, "tipo": nombre,
            "sistema": lego["sistema"], "material": mat, "capa_cad": s.capa, "origen": origen or "plano CAD",
            "inicio": list(s.a), "fin": list(s.b), "vanos": n,
            "direccion": [round((s.b[0] - s.a[0]) / largo, 6), round((s.b[1] - s.a[1]) / largo, 6)],
            "perfiles": {
                "columna": _perfil(lego["columna"], mat, "columna"),
                "viga": _perfil(lego["viga"], mat, "viga"),
                **({"diagonal": _perfil(lego["diagonal"], mat, "diagonal")} if lego["sistema"] == "arriostrado_x" else {}),
            },
            "columnas": cols, "vigas": vigas, "diagonales": diagonales,
        })
    return out


def validar(m: dict) -> list[str]:
    """Reglas de los legos de marco (tipos_marco de materialidades.yaml)."""
    errores = []
    materiales = {x["nombre"] for x in m.get("materiales", [])}
    for t in m.get("tipos_marco", []):
        n = t.get("nombre", "?")
        if t.get("sistema") not in SISTEMAS:
            errores.append(f"marco '{n}': sistema debe ser 'momento' o 'arriostrado_x'")
        if t.get("material") not in materiales:
            errores.append(f"marco '{n}': el material '{t.get('material')}' no está en la lista de materiales")
        roles = ["columna", "viga"] + (["diagonal"] if t.get("sistema") == "arriostrado_x" else [])
        for rol in roles:
            p = t.get(rol)
            if not isinstance(p, dict):
                errores.append(f"marco '{n}': falta el perfil de {rol}")
                continue
            if p.get("forma") not in FORMAS:
                errores.append(f"marco '{n}': forma de {rol} '{p.get('forma')}' inválida (usa {sorted(FORMAS)})")
            for k in ("b", "h"):
                if not (isinstance(p.get(k), (int, float)) and p[k] > 0):
                    errores.append(f"marco '{n}': {rol}.{k} debe ser un número mayor que 0 (m)")
        if t.get("vano_maximo") is not None and not (isinstance(t["vano_maximo"], (int, float)) and t["vano_maximo"] >= 1):
            errores.append(f"marco '{n}': vano_maximo debe ser al menos 1 m (o vacío)")
    nombres = {t.get("nombre") for t in m.get("tipos_marco", [])}
    for r in m.get("reglas_marco", []):
        if r.get("tipo") not in nombres:
            errores.append(f"reglas_marco: el tipo '{r.get('tipo')}' no está definido en tipos_marco")
    d = m.get("tipo_marco_por_defecto")
    if d and d not in nombres:
        errores.append(f"tipo_marco_por_defecto '{d}' no está definido en tipos_marco")
    return errores
