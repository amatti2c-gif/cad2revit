"""Pruebas de punta a punta con los planos de ejemplo generados al vuelo."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

RAIZ = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(RAIZ / "ejemplos"))
sys.path.insert(0, str(RAIZ / "parser"))

from generar_ejemplo import generar  # noqa: E402

from cad2revit.config import ErrorConfiguracion, cargar  # noqa: E402
from cad2revit.modelo import construir_modelo  # noqa: E402
from cad2revit.validar import validar  # noqa: E402


@pytest.fixture()
def proyecto(tmp_path: Path):
    """Copia entradas/ a un directorio temporal con planos de ejemplo nuevos."""
    entradas = tmp_path / "entradas"
    shutil.copytree(RAIZ / "entradas", entradas)
    generar(tmp_path / "ejemplos")

    def ajustar(archivo: str, cambios: dict) -> Path:
        ruta = entradas / archivo
        datos = yaml.safe_load(ruta.read_text(encoding="utf-8"))
        datos.update(cambios)
        ruta.write_text(yaml.safe_dump(datos, allow_unicode=True), encoding="utf-8")
        return entradas / "proyecto.yaml"

    ajustar.ruta = entradas / "proyecto.yaml"
    return ajustar


def _muros(modelo, nivel):
    return [m for m in modelo["muros"] if m["nivel"] == nivel]


def test_lod300_completo(proyecto):
    modelo = construir_modelo(cargar(proyecto.ruta))
    assert validar(modelo) == []
    assert [n["cota"] for n in modelo["niveles"]] == [0.0, 2.7, 5.4]

    n1 = _muros(modelo, "N1")
    assert len(n1) == 5
    exteriores = [m for m in n1 if m["espesor"] == 0.2]
    assert len(exteriores) == 4 and all(m["estructural"] and m["perimetral"] for m in exteriores)
    # esquinas resueltas sobre los ejes de muro
    assert {tuple(m["inicio"]) for m in exteriores} | {tuple(m["fin"]) for m in exteriores} == \
        {(0.1, 0.1), (9.9, 0.1), (0.1, 7.9), (9.9, 7.9)}
    tabique = next(m for m in n1 if m["espesor"] == 0.1)
    assert tabique["tipo"].startswith("TM-11") and not tabique["estructural"]
    assert tabique["inicio"] == [6.0, 0.1] and tabique["fin"] == [6.0, 7.9]   # unido a pesar del vano

    ab = [a for a in modelo["aberturas"] if a["id"].startswith("N1")]
    assert len(ab) == 4
    puerta = next(a for a in ab if a["bloque_cad"] == "P90")
    assert puerta["centro"] == [4.95, 0.1] and puerta["ancho"] == 0.9 and puerta["modo"] == "familia"

    assert len(modelo["columnas"]) == 4 and modelo["columnas"][0]["b"] == 0.3
    assert len(modelo["vigas"]) == 2
    assert {l["nivel"] for l in modelo["losas"]} == {"N1", "N2", "Cubierta"}
    assert len(modelo["recintos"]) == 4
    assert len(modelo["ejes"]) == 4
    # fundaciones desactivadas en nivel_detalle.yaml (ajustes.fundaciones: false)
    assert modelo["fundaciones"] == {"corridas": [], "aisladas": []}
    # tipo compuesto conserva capas
    mh = next(t for t in modelo["tipos_muro"] if t["nombre"].startswith("MH-20"))
    assert len(mh["capas"]) == 3


def test_puertas_en_tabique_delgado(proyecto):
    modelo = construir_modelo(cargar(proyecto.ruta))
    tabique = {m["id"] for m in modelo["muros"] if m["espesor"] == 0.1}
    en_tabique = [a for a in modelo["aberturas"] if a["muro"] in tabique]
    assert len(en_tabique) == 2 and all(a["modo"] == "hueco" for a in en_tabique)
    assert any("necesitan al menos 12.5 cm" in a for a in modelo["advertencias"])
    # la puerta del muro exterior (20 cm) sigue con su familia
    assert next(a for a in modelo["aberturas"] if a["bloque_cad"] == "P90")["modo"] == "familia"


def test_puertas_en_tabique_con_familia_alternativa(proyecto):
    capas = yaml.safe_load((proyecto.ruta.parent / "capas_cad.yaml").read_text(encoding="utf-8"))
    capas["aberturas_en_muros_delgados"]["puerta"].update(familia="Puerta Tabique", tipo="80x200")
    ruta = proyecto("capas_cad.yaml", capas)
    modelo = construir_modelo(cargar(ruta))
    tabique = {m["id"] for m in modelo["muros"] if m["espesor"] == 0.1}
    en_tabique = [a for a in modelo["aberturas"] if a["muro"] in tabique]
    assert all(a["modo"] == "familia" and a["familia"] == "Puerta Tabique" for a in en_tabique)


def _cajas_locales(e):
    """Cada pieza como caja (t0,t1,z0,z1) en coordenadas del muro."""
    import math
    ux, uy = e["direccion"]
    o = e["piezas"][0]["inicio"]
    cajas = []
    for p in e["piezas"]:
        t = [(q[0] - o[0]) * ux + (q[1] - o[1]) * uy for q in (p["inicio"], p["fin"])]
        z = [p["inicio"][2], p["fin"][2]]
        if abs(t[1] - t[0]) > 1e-6:          # horizontal: ancho es vertical
            cajas.append((min(t), max(t), z[0] - p["ancho"] / 2, z[0] + p["ancho"] / 2, p["rol"]))
        else:                                 # vertical: ancho va a lo largo del muro
            cajas.append((t[0] - p["ancho"] / 2, t[0] + p["ancho"] / 2, min(z), max(z), p["rol"]))
    return cajas


def test_entramado_madera(proyecto):
    modelo = construir_modelo(cargar(proyecto.ruta))
    assert validar(modelo) == []
    assert len(modelo["entramados"]) == 2                       # un tabique por planta
    e = next(x for x in modelo["entramados"] if x["muro"] == "N1-M003")
    r = e["resumen"]
    assert r["solera_superior"] == 1 and r["solera_inferior"] == 2     # la inferior se corta en la puerta
    assert r["jamba"] == 2 and r["dintel"] == 1 and "alfeizar" not in r  # puerta: sin alféizar
    assert r["pie_derecho"] >= 15 and r["cadeneta"] > 50
    assert e["material"] == "Pino radiata"
    assert any(m["nombre"] == "Pino radiata" for m in modelo["materiales"])
    # separación de pies derechos regulares: 40 cm
    ts = sorted(c[0] for c in _cajas_locales(e) if c[4] == "pie_derecho")
    pasos = [round(b - a, 3) for a, b in zip(ts, ts[1:])]
    assert pasos.count(0.4) >= 10
    # ninguna pieza se superpone con otra (tolerancia 1 mm)
    cajas = _cajas_locales(e)
    for i, a in enumerate(cajas):
        for b in cajas[i + 1:]:
            sol_t = min(a[1], b[1]) - max(a[0], b[0])
            sol_z = min(a[3], b[3]) - max(a[2], b[2])
            assert not (sol_t > 0.001 and sol_z > 0.001), f"{a} se superpone con {b}"
    # las piezas terminan bajo la losa superior (2.70 - 0.20)
    assert max(max(p["inicio"][2], p["fin"][2]) for p in e["piezas"]) <= 2.5 + 1e-6


def test_entramado_ventana_tiene_alfeizar(proyecto):
    mat = yaml.safe_load((proyecto.ruta.parent / "materialidades.yaml").read_text(encoding="utf-8"))
    # el muro de hormigón pasa a ser de madera con entramado: sus ventanas llevan alféizar
    ent = next(t for t in mat["tipos_muro"] if t.get("entramado"))["entramado"]
    mh = next(t for t in mat["tipos_muro"] if t["nombre"].startswith("MH-20"))
    mh["entramado"] = ent
    ruta = proyecto("materialidades.yaml", mat)
    modelo = construir_modelo(cargar(ruta))
    con_ventana = [e for e in modelo["entramados"] if "alfeizar" in e["resumen"]]
    assert con_ventana
    for e in con_ventana:
        cajas = _cajas_locales(e)
        for i, a in enumerate(cajas):
            for b in cajas[i + 1:]:
                assert not (min(a[1], b[1]) - max(a[0], b[0]) > 0.001 and
                            min(a[3], b[3]) - max(a[2], b[2]) > 0.001), f"{e['muro']}: {a} / {b}"


def test_sin_entramado_en_lod300_sin_ajuste(proyecto):
    ruta = proyecto("nivel_detalle.yaml", {"preset": 300, "ajustes": {}})
    assert construir_modelo(cargar(ruta))["entramados"] == []


def test_lod100_simplifica(proyecto):
    ruta = proyecto("nivel_detalle.yaml", {"preset": 100, "ajustes": {}})
    modelo = construir_modelo(cargar(ruta))
    assert validar(modelo) == []
    assert modelo["aberturas"] == [] and modelo["columnas"] == [] and modelo["vigas"] == []
    assert all(t["nombre"].startswith("Genérico") and len(t["capas"]) == 1 for t in modelo["tipos_muro"])


def test_lod350_terminaciones_y_fundaciones(proyecto):
    ruta = proyecto("nivel_detalle.yaml", {"preset": 350, "ajustes": {}})
    modelo = construir_modelo(cargar(ruta))
    assert validar(modelo) == []
    term = [l for l in modelo["losas"] if l.get("terminacion")]
    assert len(term) == 2   # N1 y N2 (la cubierta no lleva terminación separada)
    est = next(l for l in modelo["losas"] if l["id"] == "N1-L001")
    assert est["desfase"] == pytest.approx(-0.05)
    assert len(modelo["fundaciones"]["corridas"]) == 4      # muros estructurales de N1
    assert len(modelo["fundaciones"]["aisladas"]) == 2      # columnas de N1
    assert len(modelo["cielos"]) == 2
    # Revit no acepta [ ] { } : ; | < > ? ` ~ en nombres de tipos
    prohibidos = set("[]{}:;|<>?`~\\")
    assert not any(set(t["nombre"]) & prohibidos for t in modelo["tipos_losa"] + modelo["tipos_muro"])


def test_hueco_en_lod200(proyecto):
    ruta = proyecto("nivel_detalle.yaml", {"preset": 200, "ajustes": {}})
    modelo = construir_modelo(cargar(ruta))
    assert {a["modo"] for a in modelo["aberturas"]} == {"hueco"}
    mh = next(t for t in modelo["tipos_muro"] if t["nombre"].startswith("MH-20"))
    assert len(mh["capas"]) == 1 and mh["capas"][0]["material"] == "Hormigón armado H30"


def test_sobre_muros_estructurales(proyecto):
    est = yaml.safe_load((proyecto.ruta.parent / "estructuracion.yaml").read_text(encoding="utf-8"))
    est["vigas"]["sobre_muros_estructurales"] = True
    est["vigas"]["entre_columnas_alineadas"] = False
    ruta = proyecto("estructuracion.yaml", est)
    modelo = construir_modelo(cargar(ruta))
    assert len(modelo["vigas"]) == 8   # 4 muros estructurales x 2 plantas


def test_error_material_inexistente(proyecto):
    mat = yaml.safe_load((proyecto.ruta.parent / "materialidades.yaml").read_text(encoding="utf-8"))
    mat["tipos_muro"][0]["capas"][1]["material"] = "Adobe"
    ruta = proyecto("materialidades.yaml", mat)
    with pytest.raises(ErrorConfiguracion, match="Adobe"):
        cargar(ruta)


def test_ajuste_lod_invalido(proyecto):
    ruta = proyecto("nivel_detalle.yaml", {"preset": 300, "ajustes": {"muros": "bonito"}})
    with pytest.raises(ErrorConfiguracion, match="muros"):
        cargar(ruta)
