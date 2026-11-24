"""Modelo del eje: elementos sin apoyo en el plano (p. ej. viga de cumbrera o costaneras) y pilares que no llegan a
la viga por poco no deben dejar la matriz de rigidez singular."""
from app.sap.eje_revit import mallar


def _p(i, x, z1, z0=0.0):
    return {"id": i, "seccion": "100x100x3", "material": "acero", "x0": x, "z0": z0, "x1": x, "z1": z1,
            "dims": {"d": 0.1, "bf": 0.1, "t": 0.003}}


def _v(i, x0, z0, x1, z1):
    return {"id": i, "seccion": "200x100x3", "material": "acero", "tipo": "viga", "x0": x0, "z0": z0, "x1": x1, "z1": z1,
            "dims": {"d": 0.2, "bf": 0.1, "t": 0.003}}


def test_viga_sin_apoyo_y_pilar_corto():
    eje = {"eje": "T", "pilares": [_p("P1", 0.0, 3.0), _p("P2", 4.0, 3.0), _p("P3", 8.0, 2.0)],
           "vigas": [_v("V1", 0.0, 3.1, 4.0, 3.1),          # 10 cm sobre la cabeza de los pilares
                     _v("V2", -1.0, 6.0, 9.0, 6.0)],        # cumbrera: se apoya en otros ejes
           "muros": []}
    mod = mallar(eje)
    assert "V2" in mod.sin_apoyo and "P3" in mod.sin_apoyo  # P3: rotulado y suelto arriba (mecanismo)
    assert "V1" not in mod.sin_apoyo and "P1" not in mod.sin_apoyo
    assert any("se unió a la viga" in a for a in mod.avisos)
    origenes = set(mod.origen.values())
    assert {"P1", "P2", "V1"} <= origenes and not ({"V2", "P3"} & origenes)


def test_apoyos_y_uniones_a_mano():
    import pytest
    from app.sap.eje_cargas import ParametrosCargas, cargas_eje
    from app.sap.eje_pynite import analizar
    eje = {"eje": "T", "pilares": [_p("P1", 0.0, 3.0), _p("P2", 4.0, 3.0)], "vigas": [_v("V1", 0.0, 3.0, 4.0, 3.0)], "muros": []}
    pc = ParametrosCargas()
    # deslizante: solo el vertical restringido
    mod = mallar(eje, opciones={"apoyos_nudo": {"0,0": "empotrado", "4,0": "deslizante"}})
    tipos = {mod.nudos[n]: t for n, t in mod.apoyos.items()}
    assert tipos[(0.0, 0.0)] == "empotrado" and tipos[(4.0, 0.0)] == "deslizante"
    assert analizar(mod, cargas_eje(mod, pc), pc, fy=270e3 / 9.80665)["T"] > 0
    # viga rotulada en el pilar P2 (base fija): P1 empotrado da la estabilidad lateral
    mod = mallar(eje, opciones={"apoyos_nudo": {"0,0": "empotrado", "4,0": "rotula"}, "uniones": {"V1": {"j": "rotula"}}})
    assert list(mod.liberaciones.values()) == [(False, True)]
    assert analizar(mod, cargas_eje(mod, pc), pc, fy=270e3 / 9.80665)["T"] > 0
    # mecanismo real (pilar deslizante con la viga rotulada arriba): el análisis lo rechaza
    mod = mallar(eje, opciones={"apoyos_nudo": {"0,0": "empotrado", "4,0": "deslizante"}, "uniones": {"V1": {"j": "rotula"}}})
    with pytest.raises(Exception):
        analizar(mod, cargas_eje(mod, pc), pc, fy=270e3 / 9.80665)
    # quitar un apoyo («libre»)
    mod2 = mallar(eje, opciones={"apoyos_nudo": {"4,0": "libre"}})
    assert (4.0, 0.0) not in {mod2.nudos[n] for n in mod2.apoyos}


def test_pilar_mas_largo_que_el_eje_se_recorta():
    eje = {"eje": "G", "pilares": [_p("P1", 0.0, 4.95, z0=-1.65), _p("P2", 4.0, 4.95, z0=-1.65)],
           "vigas": [_v("V1", 0.0, 3.2, 4.0, 3.2)],
           "muros": [{"id": "M1", "tipo": "Muro marco plataforma", "material": "madera", "x0": 0.0, "x1": 4.0, "z0": 0.0,
                      "espesor": 0.1, "perfil": [[0.0, 3.48], [4.0, 3.48]], "vanos": []}]}
    mod = mallar(eje)
    zs = [z for _, z in mod.nudos.values()]
    assert min(zs) == 0.0 and max(zs) == 3.2         # muro y pilar unidos a la viga
    assert sum("recortado" in a for a in mod.avisos) == 2
    assert min(zs) == 0.0 and min(mod.nudos[n][1] for n in mod.apoyos) == 0.0
    sin = mallar(eje, opciones={"recortar_pilares": False})
    assert min(z for _, z in sin.nudos.values()) == -1.65


def test_fundacion_apoyos_casi_en_nudos_de_la_malla():
    """Apoyos a micrómetros de un nudo de la malla no deben crear elementos de largo casi nulo (sin equilibrio)."""
    from app.sap.fundacion import ParametrosFundacion, analizar_fundacion
    xs = [0.907 + 0.5 * i + 6e-6 for i in range(40)]               # cada 0,5 m, casi sobre la malla de 0,1 m
    apoyos = [{"nudo": f"N{i}", "x": x, "z": 0.0, "tipo": "x", "casos": {"DEAD": [0.0, 0.3, 0.0]}} for i, x in enumerate(xs)]
    o = analizar_fundacion(apoyos, {"C1_1.4D": {"DEAD": 1.4}}, {"DEAD": "DEAD"}, ParametrosFundacion())
    mu = {v["verificacion"]: v.get("Mu_tonfm") for v in o["viga"]}
    # carga casi uniforme sobre suelo elástico: momentos chicos
    assert max(abs(x or 0) for x in mu.values()) < 1.0
