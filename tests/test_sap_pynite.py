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


def test_muro_pynite_tiene_la_rigidez_del_manual():
    """PyNite toma G = E/2(1+ν) en los cuadriláteros: el modelo debe reproducir el K_eq calibrado (Ec. 2.22)."""
    from Pynite import FEModel3D
    from app.sap.eje_revit import mallar
    eje = {"tipo_archivo": "cad2revit-eje-sap", "eje": "R", "pilares": [], "vigas": [],
           "muros": [{"id": "m", "material": "madera", "x0": 0, "x1": 2.4, "z0": 0, "nucleo": 0.075,
                      "perfil": [[0, 2.5], [2.4, 2.5]], "vanos": []}]}
    mod = mallar(eje, malla=0.5)
    a = next(iter(mod.secciones_area.values()))
    E0 = mod.materiales[a["material"]]["E"]
    E_cal, G_cal = E0 * a["f11"], E0 / 2 * a["f12"]
    m = FEModel3D()
    for n, (x, z) in mod.nudos.items():
        m.add_node(n, x, z, 0)
        m.def_support(n, z == 0, z == 0, True, True, True, True)
    m.add_material("M", 2 * G_cal, G_cal, 0, 0)
    for nm, ns, _ in mod.areas:
        m.add_quad(nm, *ns, a["espesor"], "M", E_cal / (2 * G_cal), E_cal / (2 * G_cal))
    top = [n for n, (_, z) in mod.nudos.items() if abs(z - 2.5) < 1e-6]
    for n in top:
        m.add_node_load(n, "FX", 1.0 / len(top))
    m.add_load_combo("C", {"Case 1": 1})
    m.analyze_linear(check_statics=False)
    k = len(top) / sum(m.nodes[n].DX["C"] for n in top)
    assert abs(k / mod.resumen_muros[0]["K_eq_tonf_m"] - 1) < 0.15


def test_deformaciones():
    from app.sap.eje_cargas import ParametrosCargas, cargas_eje
    from app.sap.eje_pynite import analizar
    from app.sap.eje_revit import mallar
    mod = mallar(EJE)
    pc = ParametrosCargas(modos=6)
    r = analizar(mod, cargas_eje(mod, pc), pc, limites={"viga_total": 300})
    tipos = {d["tipo"] for d in r["deformaciones"]}
    assert {"Viga (vertical)", "Pilar (lateral, viento)", "Pilar (deriva sísmica)", "Muro (deriva sísmica)"} <= tipos
    assert any(d["criterio"] == "L/300" for d in r["deformaciones"])
    assert all(d["delta_mm"] >= 0 and d["limite_mm"] > 0 for d in r["deformaciones"])


def test_casos_puros_y_peso_sismico():
    from app.sap.eje_cargas import ParametrosCargas, cargas_eje
    from app.sap.eje_pynite import analizar
    from app.sap.eje_revit import mallar
    mod = mallar(EJE)
    pc = ParametrosCargas(modos=6)
    r = analizar(mod, cargas_eje(mod, pc), pc)
    casos = {c["caso"]: c for c in r["casos"]}
    assert {"DEAD", "LR", "W1", "EX+"} <= set(casos)
    # el peso sísmico es el peso propio (menos lo que cae directo en los apoyos), no el doble ni la mitad
    assert 0.9 < r["peso_sismico"] / casos["DEAD"]["reaccion_y"] <= 1.0
    assert abs(abs(casos["EX+"]["reaccion_x"]) - r["corte_basal"]) < 1e-9
    assert casos["W1"]["cargas"] and casos["W1"]["reaccion_x"] < 0


def test_masa_concentrada_por_defecto_y_consistente_equivalente():
    from app.sap.eje_cargas import ParametrosCargas, cargas_eje
    from app.sap.eje_pynite import analizar
    from app.sap.eje_revit import mallar
    mod = mallar(EJE)
    pc = ParametrosCargas(modos=6)
    c = cargas_eje(mod, pc)
    a = analizar(mod, c, pc)
    b = analizar(mod, c, pc, masa="consistente")
    assert a["masa"] == "concentrada" and b["masa"] == "consistente"
    assert abs(a["T"] / b["T"] - 1) < 0.05 and abs(a["peso_sismico"] / b["peso_sismico"] - 1) < 0.05


def test_anclaje_por_defecto_unido_al_pilar_y_hold_down_en_vanos():
    from app.sap.eje_cargas import ParametrosCargas, cargas_eje
    from app.sap.eje_pynite import analizar
    from app.sap.eje_revit import HOLD_DOWNS, mallar
    mod = mallar(EJE)
    # el segmento 0–1 llega al pilar por la izquierda y a la puerta por la derecha
    seg = {s["x0"]: s for s in mod.resumen_muros}
    assert seg[0.0]["anclaje"]["bordes"] == ["pilar", "hold_down"]
    pc = ParametrosCargas(modos=6)
    r = analizar(mod, cargas_eje(mod, pc), pc)
    for w in r["madera"]:
        tipos = {a["tipo"] for a in w["anclajes"]}
        assert tipos == {"unión al pilar", "hold-down"} and w["falta_capacidad_union"]
        assert all(a["modelo"] in HOLD_DOWNS for a in w["anclajes"] if a["tipo"] == "hold-down")
        assert w["T_anclaje"] >= 0 and w["L_prima"] < w["L"]
    # todo con hold-down: ambos bordes con hold-down y la rigidez del modelo elegido
    mod2 = mallar(EJE, opciones={"muros": {"f": {"anclaje": "hold_down", "hold_down": "HD5B"}}})
    a = mod2.resumen_muros[0]["anclaje"]
    assert a["bordes"] == ["hold_down", "hold_down"] and abs(a["k"] - 1.90 / 0.0038) < 1


def test_sismo_ex_ez_columna_fuerte_y_signo_axial():
    from app.sap.eje_cargas import ParametrosCargas, cargas_eje
    from app.sap.eje_pynite import analizar
    from app.sap.eje_revit import mallar
    mod = mallar(EJE)
    pc = ParametrosCargas(modos=6)
    r = analizar(mod, cargas_eje(mod, pc), pc)
    casos = {c["caso"]: c for c in r["casos"]}
    assert {"EX+", "EX−", "EZ+", "EZ−", "E"} <= set(casos)
    assert abs(abs(casos["EX+"]["reaccion_x"]) - r["corte_basal"]) < 1e-9
    assert casos["EX+"]["reaccion_x"] == -casos["EX−"]["reaccion_x"]
    assert abs(casos["EZ+"]["reaccion_y"]) > 0                       # el sismo vertical produce reacción vertical
    # PyNite: axial + = compresión; los pilares bajo peso propio van comprimidos (Pu > 0 en el diseño)
    assert all(a["Pu"] > 0 for a in r["acero"] if a["origen"] in ("p1", "p2"))
    cf = r["columna_fuerte"]
    assert len(cf) == 2 and all(c["razon"] > 1 and c["Puc_rel"] > 0 for c in cf)


def test_masa_participante_90_en_x_y_z():
    from app.sap.eje_cargas import ParametrosCargas, cargas_eje
    from app.sap.eje_pynite import analizar
    from app.sap.eje_revit import mallar
    mod = mallar(EJE)
    pc = ParametrosCargas(modos=6)
    r = analizar(mod, cargas_eje(mod, pc), pc)
    assert r["masa_participante_x"] >= 0.9 and r["masa_participante_z"] >= 0.9 and r["n_modos"] > 6


def test_cqc_vectorizado_igual_al_de_doble_suma():
    import numpy as np
    from app.sap.eje_pynite import _cqc
    w = [10.0, 11.0, 30.0]
    v = [np.array([1.0, -2.0]), np.array([0.5, 1.0]), np.array([-0.2, 0.3])]
    ref = np.zeros(2)
    for i in range(3):
        for j in range(3):
            r = w[j] / w[i]
            rho = 8 * 0.05 ** 2 * (1 + r) * r ** 1.5 / ((1 - r ** 2) ** 2 + 4 * 0.05 ** 2 * r * (1 + r) ** 2)
            ref = ref + rho * v[i] * v[j]
    assert np.allclose(_cqc(v, w), np.sqrt(np.abs(ref)))
