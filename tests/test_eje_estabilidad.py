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
