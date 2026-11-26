"""Modelo 3D (pestaña 6, modo 3D): malla, cargas, análisis y diseño de un galpón chico con pilares, vigas y muros."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from sap.eje_cargas import ParametrosCargas  # noqa: E402
from sap.modelo3d import analizar3d, cargas3d, mallar3d  # noqa: E402

DIMS = {"d": 0.2, "bf": 0.1, "t": 0.004}


def galpon(con_muros=True):
    pil = []
    for i, (x, y) in enumerate([(0, 0), (6, 0), (6, 4), (0, 4)]):
        pil.append({"id": f"P{i}", "seccion": "200x100x4", "material": "acero", "p0": [x, y, 0], "p1": [x, y, 3],
                    "giro": 0, "dims": DIMS})
    vig = []
    esq = [(0, 0), (6, 0), (6, 4), (0, 4)]
    for i in range(4):
        a, b = esq[i], esq[(i + 1) % 4]
        vig.append({"id": f"V{i}", "seccion": "200x100x4", "material": "acero", "tipo": "viga", "costanera": False,
                    "p0": [a[0], a[1], 3], "p1": [b[0], b[1], 3], "giro": 0, "dims": DIMS})
    vig.append({"id": "C1", "seccion": "C", "material": "acero", "tipo": "viga", "costanera": True,
                "p0": [0, 2, 3], "p1": [6, 2, 3], "giro": 0, "dims": DIMS})
    muros = []
    if con_muros:
        muros.append({"id": "M1", "tipo": "Marco plataforma", "material": "madera", "a": [0, 0], "b": [6, 0], "largo": 6,
                      "z0": 0, "espesor": 0.12, "nucleo": 0.09, "perfil": [[0, 3], [6, 3]],
                      "vanos": [{"t0": 2, "t1": 3, "z0": 0, "z1": 2.1, "puerta": True}]})
        muros.append({"id": "M2", "tipo": "Marco plataforma", "material": "madera", "a": [6, 0], "b": [6, 4], "largo": 4,
                      "z0": 0, "espesor": 0.12, "nucleo": 0.09, "perfil": [[0, 3], [4, 3]], "vanos": []})
    return {"tipo_archivo": "cad2revit-modelo-3d", "nombre": "galpón", "pilares": pil, "vigas": vig, "muros": muros}


def test_malla_3d():
    mod = mallar3d(galpon(), malla=0.5)
    assert mod.areas and mod.barras
    assert "C1" in mod.excluidos_auto
    # los nudos de los muros comparten los de pilares y vigas en sus bordes
    for nombre, ns, _ in mod.areas:
        assert all(n in mod.nudos for n in ns)
    assert not mod.sin_apoyo


def test_analisis_3d_marco():
    mod = mallar3d(galpon(False))
    c = cargas3d(mod, ParametrosCargas())
    r = analizar3d(mod, c, ParametrosCargas())
    assert r["acero"] and r["masa_participante"]["X"] > 0.85 and r["masa_participante"]["Y"] > 0.85
    # equilibrio vertical en D: la reacción suma el peso
    d = next(x for x in r["casos"] if x["caso"] == "DEAD")
    assert d["Rz"] > 0
    assert all(a["razon"] >= 0 for a in r["acero"])


def test_analisis_3d_con_muros():
    mod = mallar3d(galpon())
    c = cargas3d(mod, ParametrosCargas())
    r = analizar3d(mod, c, ParametrosCargas())
    assert r["madera"], "debe verificar los muros"
    assert all(m["FU"] >= 0 for m in r["madera"])
    # los muros rigidizan: el periodo fundamental baja respecto al marco solo
    r0 = analizar3d(mallar3d(galpon(False)), cargas3d(mallar3d(galpon(False)), ParametrosCargas()), ParametrosCargas())
    assert r["periodos"][0] < r0["periodos"][0]


def _portico(con_longitudinal=True):
    """Pórticos con la viga inclinada 2,5 cm al lado de los pilares y 5 cm sobre su cabeza (como en Revit), tubos
    circulares sin espesor y vigas «300x100x3» con dimensiones de familia que no calzan con el nombre."""
    pil, vig = [], []
    for i, x in enumerate((0.0, 3.0, 6.0)):
        pil.append({"id": f"A{i}", "seccion": "PILAR CIRCULAR D240mm", "p0": [x, 0, 0.2], "p1": [x, 0, 2.6065], "dims": {}})
        pil.append({"id": f"B{i}", "seccion": "PILAR CIRCULAR D140mm", "p0": [x, -3.334, 0.2], "p1": [x, -3.334, 3.085], "dims": {}})
        vig.append({"id": f"T{i}", "seccion": "300x100x3", "tipo": "viga", "p0": [x + 0.025, 0, 2.655], "p1": [x + 0.025, -9.975, 4.09],
                    "giro": 90, "dims": {"d": 0.05, "bf": 0.15, "t": 0.003}})
    if con_longitudinal:
        vig.append({"id": "L1", "seccion": "300x100x3", "tipo": "viga", "p0": [0, 0.026, 2.655], "p1": [6, 0.026, 2.655], "dims": {}})
        vig.append({"id": "L2", "seccion": "300x100x3", "tipo": "viga", "p0": [0, -3.31, 3.135], "p1": [6, -3.31, 3.135], "dims": {}})
    return {"pilares": pil, "vigas": vig, "muros": []}


def test_uniones_con_excentricidad():
    mod = mallar3d(_portico())
    assert not mod.sin_apoyo
    assert mod.secciones["PILAR_CIRCULAR_D240mm"]["forma"] == "Pipe"
    assert mod.secciones["300x100x3"] == {"forma": "Box", "material": "ACERO", "d": 0.3, "bf": 0.1, "t": 0.003}
    r = analizar3d(mod, cargas3d(mod, ParametrosCargas()), ParametrosCargas())
    assert r["acero"]


def test_mecanismo_explicado():
    import pytest
    from sap.modelo3d import ModeloInestable
    mod = mallar3d(_portico(False))
    with pytest.raises(ModeloInestable, match="dirección X"):
        analizar3d(mod, cargas3d(mod, ParametrosCargas()), ParametrosCargas())
