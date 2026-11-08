"""Cotas de nivel leídas desde elevaciones / cortes."""
from __future__ import annotations

import re

from .dxf_reader import DibujoCAD


def cota_en_texto(texto: str, rx: re.Pattern) -> float | None:
    """Cota de nivel escrita en un texto ("NPT +2.70", "N +3,10"), o None.
    La N no puede venir pegada a otra letra ("VOLCAN 610" no es una cota) y la cota debe ser razonable."""
    for m in rx.finditer(texto):
        if m.start() > 0 and texto[m.start() - 1].isalpha():
            continue
        try:
            v = float(m.group(1).replace(",", "."))
        except (ValueError, IndexError):
            continue
        if abs(v) <= 200:
            return v
    return None


def leer_cotas(dibujos: list[DibujoCAD], patron: str, capa: re.Pattern | None) -> list[float]:
    rx = re.compile(patron)
    cotas = set()
    for d in dibujos:
        textos = d.textos_en(capa) if capa else []
        if not textos:  # si la capa no existe, buscar en todo el dibujo
            textos = d.textos
        for t in textos:
            v = cota_en_texto(t.texto, rx)
            if v is not None:
                cotas.add(round(v, 3))
    return sorted(cotas)


def ajustar_niveles(niveles: list[dict], cotas: list[float], advertencias: list[str]) -> list[dict]:
    niveles = sorted((dict(n) for n in niveles), key=lambda n: n["cota"])
    if not cotas:
        return niveles
    if len(cotas) != len(niveles):
        advertencias.append(f"Las elevaciones tienen {len(cotas)} cotas de nivel {cotas} pero el proyecto "
                            f"define {len(niveles)} niveles; se mantienen las cotas de proyecto.yaml")
        return niveles
    for n, c in zip(niveles, cotas):
        if abs(n["cota"] - c) > 0.005:
            advertencias.append(f"Nivel {n['nombre']}: cota {n['cota']} reemplazada por {c} (leída de elevación)")
        n["cota"] = c
    return niveles
