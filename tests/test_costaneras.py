"""Costaneras (elementos secundarios): perfiles conformados en frío AISI S100-16 y cargas NCh."""
import math

import numpy as np

from app.sap.costaneras import (ParametrosCostaneras, analizar_costaneras, corte_nominal, fn_volcamiento, gcp,
                                leer_perfil, propiedades_brutas, seccion_efectiva, viga_continua)


def _techo(perfil="CA 150x50x15x2", n=6, sep=1.0, pend=15.0, vigas_x=(0, 5, 10), L=10.0):
    tg, z0 = math.tan(math.radians(pend)), 3.0
    cost = []
    for i in range(n):
        y = 0.1 + i * sep * math.cos(math.radians(pend))
        z = z0 + y * tg + 0.15
        cost.append({"id": 100 + i, "sistema": 1, "tipo": f"Costanera: {perfil}", "p0": [0, y, z], "p1": [L, y, z]})
    ymax = 0.2 + n * sep
    vigas = [{"id": 10 + k, "tipo": "viga", "p0": [x, 0, z0], "p1": [x, ymax, z0 + ymax * tg]} for k, x in enumerate(vigas_x)]
    return {"tipo_archivo": "cad2revit-costaneras", "z_base": 0.0,
            "sistemas": [{"id": 1, "nombre": "Techo", "contorno": [[0, 0, z0], [L, 0, z0], [L, ymax, z0], [0, ymax, z0]]}],
            "costaneras": cost, "vigas": vigas}


def test_perfil_ca_propiedades():
    pf = leer_perfil("CA 150x50x15x2")
    assert (pf.tipo, pf.h, pf.b, pf.c, pf.t) == ("CA", 150, 50, 15, 2)
    pr = propiedades_brutas(pf)
    assert abs(pr["A"] / 100 - 5.47) < 0.1                 # catálogo: 5,47 cm²
    assert abs(pr["Ix"] / 1e4 - 187) < 6                   # catálogo: ≈ 187 cm⁴
    se = seccion_efectiva(pf, "x", 1, 270, 270)
    assert abs(se["Se"] - pr["Sx"]) / pr["Sx"] < 0.01      # 2 mm: ala con labio y alma totalmente efectivas a 270 MPa
    assert leer_perfil("Z 200x70x20x2,5").tipo == "ZA"
    assert leer_perfil("Tubular 100x50x2").tipo == "TUB"
    assert leer_perfil("C 100x50x3").tipo == "C"
    assert leer_perfil("sin numeros") is None


def test_ancho_efectivo_reduce_seccion_esbelta():
    pf = leer_perfil("CA 250x50x10x1")                     # alma y ala esbeltas
    pr = propiedades_brutas(pf)
    se = seccion_efectiva(pf, "x", 1, 270, 270)
    assert se["Se"] < 0.9 * pr["Sx"]


def test_volcamiento_y_corte():
    pf = leer_perfil("CA 150x50x15x2")
    pr = propiedades_brutas(pf)
    largos = [fn_volcamiento(pf, pr, L, 1.0, 270)["Fn"] for L in (1000, 2000, 4000)]
    assert largos[0] > largos[1] > largos[2]
    assert fn_volcamiento(pf, pr, 2000, 1.67, 270)["Fn"] > largos[1]
    assert 30e3 < corte_nominal(pf, pr, 270) < 60e3


def test_viga_continua_dos_tramos():
    xs = np.linspace(0, 10000, 41)
    q = np.full(40, -1.0)                                  # 1 N/mm hacia abajo
    M, V, v, R = viga_continua(xs, {0, 20, 40}, q, 2e5 * 1e6)
    assert abs(M[20] + 1.0 * 5000 ** 2 / 8) < 1e-3 * 5000 ** 2 / 8   # −wL²/8 en el apoyo central
    assert abs(R[20] - 1.25 * 5000) < 1                    # 5/4·wL
    assert abs(R[0] - 0.375 * 5000) < 1


def test_gcp_area_y_zonas():
    assert gcp(15, 1, 0.5) == -0.9 and abs(gcp(15, 1, 20) + 0.8) < 1e-9
    assert gcp(15, 3, 5) < gcp(15, 1, 5)
    assert gcp(5, "pos", 0.1) == 0.3


def test_costaneras_techo_sintetico():
    r = analizar_costaneras(_techo(), ParametrosCostaneras(ids=list(range(100, 106))))
    cs = {c["id"]: c for c in r["costaneras"]}
    assert len(cs) == 6
    c = cs[102]
    assert [round(t["luz"], 2) for t in c["tramos"]] == [5.0, 5.0]
    assert abs(c["s"] - 1.0) < 1e-6 and abs(c["theta"] - 15) < 0.1
    assert [t["tensores"] for t in c["tramos"]] == [1, 1]    # auto: L/2 hasta 6 m
    # 1,2D + 1,6Lr + 0,8W+: momento en el apoyo central ≈ q·L²/8
    v = next(x for x in c["verificaciones"] if x["verificacion"].startswith("Flexión biaxial"))
    assert 0.3 < v["razon"] < 1.2
    assert cs[100]["s"] < cs[102]["s"]                      # costanera de borde
    assert all(x["FU"] is not None for x in r["costaneras"])


def test_tensores_l3_y_perfil_dado():
    r = analizar_costaneras(_techo(perfil="X"), ParametrosCostaneras(tensores="L/3", perfiles={"Costanera: X": "CA 200x50x15x2"}, ids=[102]))
    assert len(r["costaneras"]) == 1                        # solo las elegidas
    c = r["costaneras"][0]
    assert abs(c["s"] - 1.0) < 1e-6                         # las no elegidas igual dan el ancho colaborante
    assert c["perfil"] == "CA 200x50x15x2"
    assert [t["tensores"] for t in c["tramos"]] == [2, 2] and len(c["tensores_x"]) == 4


def test_sin_apoyos():
    d = _techo()
    d["vigas"] = d["vigas"][:1]
    r = analizar_costaneras(d, ParametrosCostaneras(ids=[100, 101, 102, 103, 104, 105]))
    assert all(c["FU"] is None and c["notas"] for c in r["costaneras"])


def test_sin_eleccion():
    import pytest
    with pytest.raises(ValueError):
        analizar_costaneras(_techo())


def test_solo_tramo_de_mayor_luz():
    r = analizar_costaneras(_techo(vigas_x=(0, 4, 10)), ParametrosCostaneras(ids=[102]))
    c = r["costaneras"][0]
    assert abs(c["tramo_verificado"]["luz"] - 6.0) < 1e-6 and c["tramo_verificado"]["n"] == 2
    assert [t["verificado"] for t in c["tramos"]] == [False, True]
    for v in c["verificaciones"]:
        assert 4.0 - 1e-6 <= v["x"] <= 10.0 + 1e-6
