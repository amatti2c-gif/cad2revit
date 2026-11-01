"""Puertas y ventanas: bloques del CAD -> aberturas alojadas en muros."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .dxf_reader import Insercion, Punto
from .muros import Muro, dist_punto_segmento, proyectar


@dataclass
class Abertura:
    id: str
    categoria: str          # puerta | ventana
    insercion: Insercion
    familia: str | None
    tipo: str | None
    ancho: float | None
    alto: float
    antepecho: float
    muro: str | None = None
    centro: Punto | None = None
    modo: str | None = None      # None = el del nivel de detalle; "hueco" = forzar vano


def regla_bloque(nombre: str, reglas: list[dict]) -> dict | None:
    for r in reglas:
        if re.search(r["bloque"], nombre):
            return r
    return None


def clasificar_inserciones(inserciones: list[Insercion], cfg) -> tuple[list[tuple[Insercion, dict]], list[Insercion]]:
    """Devuelve (aberturas con su regla, columnas)."""
    reglas = cfg.capas_cad.get("bloques") or []
    cp, cv, cc = cfg.capa("puertas"), cfg.capa("ventanas"), cfg.capa("columnas")
    aberturas, columnas = [], []
    for ins in inserciones:
        r = regla_bloque(ins.nombre, reglas)
        cat = r["categoria"] if r else None
        if cat is None:
            if cp and cp.search(ins.capa):
                cat = "puerta"
            elif cv and cv.search(ins.capa):
                cat = "ventana"
            elif cc and cc.search(ins.capa):
                cat = "columna"
        if cat in ("puerta", "ventana"):
            aberturas.append((ins, r or {"categoria": cat}))
        elif cat == "columna":
            columnas.append(ins)
    return aberturas, columnas


def asignar_a_muros(items: list[tuple[Insercion, dict]], muros: list[Muro], cfg, prefijo: str,
                    advertencias: list[str]) -> list[Abertura]:
    nd = cfg.nivel_detalle
    out = []
    for k, (ins, regla) in enumerate(items, 1):
        cat = regla["categoria"]
        gen = nd.get("familia_puerta_generica" if cat == "puerta" else "familia_ventana_generica") or {}
        ab = Abertura(
            id=f"{prefijo}-{'P' if cat == 'puerta' else 'V'}{k:03d}",
            categoria=cat, insercion=ins,
            familia=regla.get("familia") or gen.get("familia"),
            tipo=regla.get("tipo") or gen.get("tipo"),
            ancho=regla.get("ancho"),
            alto=float(regla.get("alto", 2.10 if cat == "puerta" else 1.20)),
            antepecho=float(regla.get("antepecho", 0.0 if cat == "puerta" else 0.90)),
        )
        # Muro más cercano al punto de inserción
        candidatos = sorted(muros, key=lambda m: dist_punto_segmento(ins.punto, m.a, m.b))
        muro = next((m for m in candidatos
                     if dist_punto_segmento(ins.punto, m.a, m.b) <= m.espesor / 2 + 0.15), None)
        if muro is None:
            advertencias.append(f"{ab.id} ({ins.nombre}) en {ins.punto}: no se encontró muro anfitrión; omitida")
            continue
        u = muro.direccion
        pts = ins.esquinas or ([ins.extremos[0], ins.extremos[1]] if ins.extremos else [ins.punto])
        ts = [proyectar(p, muro.a, u) for p in pts]
        t0, t1 = min(ts), max(ts)
        ancho_medido = t1 - t0
        if ab.ancho is None:
            ab.ancho = round(ancho_medido, 3) if ancho_medido > 0.3 else (0.9 if cat == "puerta" else 1.2)
        tc = (t0 + t1) / 2 if ancho_medido > 0.3 else proyectar(ins.punto, muro.a, u)
        tc = max(ab.ancho / 2, min(muro.largo - ab.ancho / 2, tc))
        ab.centro = (round(muro.a[0] + u[0] * tc, 4), round(muro.a[1] + u[1] * tc, 4))
        ab.muro = muro.id
        _regla_muro_delgado(ab, muro, cfg, advertencias)
        out.append(ab)
    return out


def _regla_muro_delgado(ab: Abertura, muro: Muro, cfg, advertencias: list[str]) -> None:
    """Las familias de puerta/ventana de Revit exigen un espesor mínimo de muro (las de la
    plantilla métrica de Revit 2027: 12,5 cm). En muros más delgados se usa la familia
    alternativa configurada o, si no hay, se deja solo el vano."""
    reglas = cfg.capas_cad.get("aberturas_en_muros_delgados") or {}
    r = reglas.get(ab.categoria) or {}
    minimo = r.get("espesor_minimo")
    if not minimo:
        return
    tipo = cfg.tipo_muro(muro.tipo) if muro.tipo else None
    espesor = tipo["espesor_total"] if tipo else muro.espesor
    if espesor >= minimo - 1e-6:
        return
    if r.get("familia"):
        ab.familia, ab.tipo = r["familia"], r.get("tipo")
        advertencias.append(f"{ab.id}: muro {muro.id} de {espesor * 100:.1f} cm (< {minimo * 100:.1f} cm); "
                            f"se usa la familia para muros delgados '{ab.familia}'")
    else:
        ab.modo = "hueco"
        advertencias.append(f"{ab.id}: la {ab.categoria} va en el muro {muro.id} de {espesor * 100:.1f} cm y las "
                            f"familias configuradas necesitan al menos {minimo * 100:.1f} cm; se deja solo el vano. "
                            "Define una familia para muros delgados en capas_cad.yaml (aberturas_en_muros_delgados).")
