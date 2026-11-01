"""Cotas de nivel leídas desde elevaciones / cortes."""
from __future__ import annotations

import re

from .dxf_reader import DibujoCAD


def leer_cotas(dibujos: list[DibujoCAD], patron: str, capa: re.Pattern | None) -> list[float]:
    rx = re.compile(patron)
    cotas = set()
    for d in dibujos:
        textos = d.textos_en(capa) if capa else []
        if not textos:  # si la capa no existe, buscar en todo el dibujo
            textos = d.textos
        for t in textos:
            m = rx.search(t.texto)
            if m:
                cotas.add(round(float(m.group(1).replace(",", ".")), 3))
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
