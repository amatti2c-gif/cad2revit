"""Genera planos DXF de ejemplo (casa de 2 pisos de 10 x 8 m) para probar el
flujo completo sin tener planos reales.

    python ejemplos/generar_ejemplo.py            # escribe en ejemplos/
"""
from __future__ import annotations

import sys
from pathlib import Path

import ezdxf


def _linea_con_vanos(msp, capa, fijo, desde, hasta, vanos, horizontal):
    """Dibuja una línea de cara de muro interrumpida por vanos [(a, b), ...]."""
    cortes = [desde]
    for a, b in sorted(vanos):
        cortes += [a, b]
    cortes.append(hasta)
    for a, b in zip(cortes[::2], cortes[1::2]):
        if b - a > 1e-6:
            p, q = ((a, fijo), (b, fijo)) if horizontal else ((fijo, a), (fijo, b))
            msp.add_line(p, q, dxfattribs={"layer": capa})


def _bloques(doc):
    p90 = doc.blocks.new("P90")
    p90.add_line((0, 0), (0, 0.9))
    p90.add_arc((0, 0), 0.9, 0, 90)
    p80 = doc.blocks.new("P80")
    p80.add_line((0, 0), (0, 0.8))
    p80.add_arc((0, 0), 0.8, 0, 90)
    v120 = doc.blocks.new("V120")
    v120.add_lwpolyline([(0, 0), (1.2, 0), (1.2, 0.2), (0, 0.2)], close=True)
    v120.add_line((0, 0.1), (1.2, 0.1))


def planta(ruta: Path, con_puerta_exterior: bool) -> None:
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 6  # metros
    for capa in ("A-MURO-HA", "A-TABIQUE", "A-PUERTA", "A-VENTANA", "S-COL", "A-EJES", "A-RECINTO"):
        doc.layers.add(capa)
    _bloques(doc)
    msp = doc.modelspace()
    E = "A-MURO-HA"

    # Vanos en muros exteriores (x o y a lo largo del muro)
    sur = [(4.5, 5.4)] if con_puerta_exterior else [(4.2, 5.4)]
    norte = [(2.0, 3.2)]
    este = [(3.0, 4.2)]

    # Muros exteriores: cara exterior [0,10]x[0,8], interior [0.2,9.8]x[0.2,7.8], e = 0.20
    _linea_con_vanos(msp, E, 0.0, 0.0, 10.0, sur, True)                 # sur ext
    _linea_con_vanos(msp, E, 0.2, 0.2, 5.95, sur, True)                 # sur int (cortada por tabique)
    _linea_con_vanos(msp, E, 0.2, 6.05, 9.8, [], True)
    _linea_con_vanos(msp, E, 8.0, 0.0, 10.0, norte, True)               # norte ext
    _linea_con_vanos(msp, E, 7.8, 0.2, 5.95, norte, True)
    _linea_con_vanos(msp, E, 7.8, 6.05, 9.8, [], True)
    _linea_con_vanos(msp, E, 0.0, 0.0, 8.0, [], False)                  # oeste
    _linea_con_vanos(msp, E, 0.2, 0.2, 7.8, [], False)
    _linea_con_vanos(msp, E, 10.0, 0.0, 8.0, este, False)               # este
    _linea_con_vanos(msp, E, 9.8, 0.2, 7.8, este, False)
    # Jambas de los vanos
    for a, b in sur:
        for x in (a, b):
            msp.add_line((x, 0), (x, 0.2), dxfattribs={"layer": E})
    for a, b in norte:
        for x in (a, b):
            msp.add_line((x, 7.8), (x, 8.0), dxfattribs={"layer": E})
    for a, b in este:
        for y in (a, b):
            msp.add_line((9.8, y), (10, y), dxfattribs={"layer": E})

    # Tabique interior x = 6.0, e = 0.10, con puerta y 3.0-3.8
    _linea_con_vanos(msp, "A-TABIQUE", 5.95, 0.2, 7.8, [(3.0, 3.8)], False)
    _linea_con_vanos(msp, "A-TABIQUE", 6.05, 0.2, 7.8, [(3.0, 3.8)], False)

    # Aberturas
    if con_puerta_exterior:
        msp.add_blockref("P90", (4.5, 0.2), dxfattribs={"layer": "A-PUERTA"})
    else:
        msp.add_blockref("V120", (4.2, 0.0), dxfattribs={"layer": "A-VENTANA"})
    msp.add_blockref("P80", (6.05, 3.0), dxfattribs={"layer": "A-PUERTA", "rotation": 90})
    msp.add_blockref("V120", (2.0, 7.8), dxfattribs={"layer": "A-VENTANA"})
    msp.add_blockref("V120", (10.0, 3.0), dxfattribs={"layer": "A-VENTANA", "rotation": 90})

    # Columnas de terraza (polilíneas cerradas 0.30 x 0.30)
    for cx in (2.0, 8.0):
        msp.add_lwpolyline([(cx - .15, -2.15), (cx + .15, -2.15), (cx + .15, -1.85), (cx - .15, -1.85)],
                           close=True, dxfattribs={"layer": "S-COL"})

    # Marcos de acero (eje en planta): de momento al norte y arriostrado al oeste
    for capa in ("S-MARCO-MOMENTO", "S-MARCO-ARRIOSTRADO"):
        doc.layers.add(capa)
    msp.add_line((0, 10), (10, 10), dxfattribs={"layer": "S-MARCO-MOMENTO"})       # 10 m -> 2 vanos
    msp.add_line((-2, 0), (-2, 6), dxfattribs={"layer": "S-MARCO-ARRIOSTRADO"})    # 6 m -> 1 vano

    # Ejes
    for x in (0.1, 6.0, 9.9):                 # el eje 6.0 va por el tabique
        msp.add_line((x, -3), (x, 9), dxfattribs={"layer": "A-EJES"})
    for y in (0.1, 7.9):
        msp.add_line((-1, y), (11, y), dxfattribs={"layer": "A-EJES"})

    # Recintos
    msp.add_text("ESTAR-COMEDOR", dxfattribs={"layer": "A-RECINTO", "insert": (3.0, 4.0), "height": 0.2})
    msp.add_text("DORMITORIO", dxfattribs={"layer": "A-RECINTO", "insert": (8.0, 5.0), "height": 0.2})
    doc.saveas(ruta)


def elevacion(ruta: Path) -> None:
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 6
    doc.layers.add("A-NIVEL")
    msp = doc.modelspace()
    for cota, txt in ((0.0, "NPT +0.00"), (2.7, "NPT +2.70"), (5.4, "NPT +5.40")):
        msp.add_line((-1, cota), (11, cota), dxfattribs={"layer": "A-NIVEL"})
        msp.add_text(txt, dxfattribs={"layer": "A-NIVEL", "insert": (11.2, cota), "height": 0.2})
    doc.saveas(ruta)


def generar(destino: Path) -> None:
    destino.mkdir(parents=True, exist_ok=True)
    planta(destino / "planta_n1.dxf", con_puerta_exterior=True)
    planta(destino / "planta_n2.dxf", con_puerta_exterior=False)
    elevacion(destino / "elevacion_norte.dxf")


if __name__ == "__main__":
    generar(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent)
    print("Planos de ejemplo generados.")
