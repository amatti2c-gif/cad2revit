"""Análisis del eje en la nube con PyNite y verificaciones de acero, madera y hormigón."""

import io
import json
import math

import pytest

pytest.importorskip("Pynite")

DIMS_W8X31 = {"d": 0.203, "bf": 0.203, "tw": 0.0072, "tf": 0.011}
DIMS_W8X10 = {"d": 0.2, "bf": 0.1, "tw": 0.0043, "tf": 0.0052}
EJE = {
    "tipo_archivo": "cad2revit-eje-sap", "eje": "T", "ancho_tributario": 3.0,
    "pilares": [
        {"id": "p1", "seccion": "W8X31", "material": "acero", "x0": 0, "z0": 0, "x1": 0, "z1": 3.0, "dims": DIMS_W8X31},
        {"id": "p2", "seccion": "W8X31", "material": "acero", "x0": 6.0, "z0": 0, "x1": 6.0, "z1": 3.0, "dims": DIMS_W8X31},
    ],
    "vigas": [
        {"id": "t1", "seccion": "W8X10", "material": "acero", "x0": 0, "z0": 3.0, "x1": 3, "z1": 4.5, "dims": DIMS_W8X10},
        {"id": "t2", "seccion": "W8X10", "material": "acero", "x0": 3, "z0": 4.5, "x1": 6, "z1": 3.0, "dims": DIMS_W8X10},
    ],
    "muros": [{"id": "f", "material": "madera", "x0": 0, "x1": 6, "z0": 0, "nucleo": 0.075,
               "perfil": [[0, 3.0], [3, 4.5], [6, 3.0]], "vanos": [{"x0": 1.0, "x1": 2.2, "z0": 0, "z1": 2.1}]}],
}


def test_analisis_pynite_eje_con_fronton():
    from app.sap.eje_cargas import ParametrosCargas, cargas_eje
    from app.sap.eje_pynite import analizar
    from app.sap.eje_revit import mallar
    mod = mallar(EJE)
    pc = ParametrosCargas(modos=6)
    r = analizar(mod, cargas_eje(mod, pc), pc)
    assert 0 < r["T"] < 1 and r["masa_participante_x"] > 0.8
    assert r["q_min"] - 1e-9 <= r["corte_basal"] <= r["q_max"] + 1e-9
    assert {a["origen"] for a in r["acero"]} == {"p1", "p2", "t1", "t2"}
    assert all(0 < a["razon"] < 1 and a["combo"] for a in r["acero"])
    assert len(r["madera"]) == 2 and all(m["FU"] > 0 for m in r["madera"])
    json.dumps(r)


def test_acero_aisc_w8x31():
    from app.sap.diseno_acero import Perfil, capacidades, razon
    p = Perfil("I", **DIMS_W8X31)
    g = p.props()
    assert math.isclose(g["A"], 57.7e-4, rel_tol=0.03)
    cap = capacidades(p, 3.0, 3.0, 3.0)
    assert cap["phiPn"] > 100 and cap["phiMn"] > 10
    assert razon(0.2 * cap["phiPn"], 0, 0, cap)["ecuacion"] == "H1-1a"


def test_hormigon_aci():
    from app.sap import diseno_hormigon as dh
    v = dh.flexion(300, 500, 450, 942, 25, 420)
    a = 942 * 420 / (0.85 * 25 * 300)
    assert math.isclose(v["phiMn_kNm"], 0.9 * 942 * 420 * (450 - a / 2) / 1e6, rel_tol=1e-3)
    pts = dh.interaccion(400, 400, [(1206, 60), (1206, 340)], 28, 420)
    assert dh.dentro_de_interaccion(pts, 1000, 120)["razon"] < 1


def test_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD2REVIT_DATOS", str(tmp_path))
    from fastapi.testclient import TestClient
    from app.servidor import app
    cli = TestClient(app)
    pid = cli.post("/api/proyectos", json={"nombre": "prueba sap"}).json()["id"]
    arch = {"archivo": ("eje.sap.json", io.BytesIO(json.dumps(EJE).encode()), "application/json")}
    nombre = cli.post(f"/api/proyectos/{pid}/sap", files=arch).json()["nombre"]
    r = cli.post(f"/api/proyectos/{pid}/sap/{nombre}/analizar", json={"parametros": {"cargas": {"modos": 6}}, "opciones": {}})
    assert r.status_code == 200, r.text
    assert cli.get(f"/api/proyectos/{pid}/sap/{nombre}/resultado").json()["acero"]
    h = cli.post("/api/hormigon/verificar", json={"b": 300, "h": 500, "recubrimiento": 50, "as_inf": 942, "av": 157, "s": 150,
                                                 "Mu": 100, "Vu": 80}).json()
    assert 0 < h["flexion"]["razon"] < 1 and h["corte"]["phiVn_kN"] > 80
