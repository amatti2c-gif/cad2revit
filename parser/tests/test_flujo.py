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


def _con_familias(proyecto) -> None:
    """Estas pruebas revisan las familias de puerta/ventana (por defecto la app deja solo vanos)."""
    nd = yaml.safe_load((proyecto.ruta.parent / "nivel_detalle.yaml").read_text(encoding="utf-8"))
    proyecto("nivel_detalle.yaml", {"ajustes": dict(nd.get("ajustes") or {}, aberturas="familia")})


def _muros(modelo, nivel):
    return [m for m in modelo["muros"] if m["nivel"] == nivel]


def test_lod300_completo(proyecto):
    _con_familias(proyecto)
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
    assert len(modelo["ejes"]) == 5
    # fundaciones desactivadas en nivel_detalle.yaml (ajustes.fundaciones: false)
    assert modelo["fundaciones"] == {"corridas": [], "aisladas": []}
    # tipo compuesto conserva capas
    mh = next(t for t in modelo["tipos_muro"] if t["nombre"].startswith("MH-20"))
    assert len(mh["capas"]) == 3


def test_puertas_en_tabique_delgado(proyecto):
    _con_familias(proyecto)
    modelo = construir_modelo(cargar(proyecto.ruta))
    tabique = {m["id"] for m in modelo["muros"] if m["espesor"] == 0.1}
    en_tabique = [a for a in modelo["aberturas"] if a["muro"] in tabique]
    assert len(en_tabique) == 2 and all(a["modo"] == "hueco" for a in en_tabique)
    assert any("necesitan al menos 12.5 cm" in a for a in modelo["advertencias"])
    # la puerta del muro exterior (20 cm) sigue con su familia
    assert next(a for a in modelo["aberturas"] if a["bloque_cad"] == "P90")["modo"] == "familia"


def test_puertas_en_tabique_con_familia_alternativa(proyecto):
    _con_familias(proyecto)
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


def test_legos_por_capa(proyecto):
    py = yaml.safe_load(proyecto.ruta.read_text(encoding="utf-8"))
    py["legos"] = {"muros": {"A-TABIQUE": "TB-10 Tabique metalcon 10"},
                   "losa": "L-15 Losa HA 15 (sin terminación)"}
    ruta = proyecto("proyecto.yaml", py)
    modelo = construir_modelo(cargar(ruta))
    tabique = next(m for m in modelo["muros"] if m["capa_cad"] == "A-TABIQUE")
    assert tabique["tipo"].startswith("TB-10")
    assert modelo["entramados"] == []          # el metalcon no lleva entramado de madera
    assert {l["tipo"] for l in modelo["losas"] if not l["cubierta"]} == {"L-15 Losa HA 15 (sin terminación)"}


def test_lego_inexistente(proyecto):
    py = yaml.safe_load(proyecto.ruta.read_text(encoding="utf-8"))
    py["legos"] = {"muros": {"A-TABIQUE": "No existe"}}
    with pytest.raises(ErrorConfiguracion, match="No existe"):
        cargar(proyecto("proyecto.yaml", py))


def test_marcos_de_acero(proyecto):
    modelo = construir_modelo(cargar(proyecto.ruta))
    assert validar(modelo) == []
    marcos = [f for f in modelo["marcos"] if f["origen"] == "plano CAD"]
    assert len(marcos) == 4                                   # 2 marcos dibujados x 2 pisos
    mom = next(f for f in marcos if f["nivel"] == "N1" and f["sistema"] == "momento")
    arr = next(f for f in marcos if f["nivel"] == "N1" and f["sistema"] == "arriostrado_x")
    # momento: 10 m con vano máximo 6 -> 2 vanos, 3 columnas, 2 vigas, sin diagonales
    assert mom["vanos"] == 2 and len(mom["columnas"]) == 3 and len(mom["vigas"]) == 2 and mom["diagonales"] == []
    assert mom["perfiles"]["viga"]["perfil"] == "W310X21" and "diagonal" not in mom["perfiles"]
    # arriostrado: 6 m -> 1 vano, cruz de 2 diagonales de piso a piso
    assert arr["vanos"] == 1 and len(arr["diagonales"]) == 2 and arr["perfiles"]["diagonal"]["forma"] == "C"
    d = arr["diagonales"][0]
    assert d["inicio"][2] == 0.0 and d["fin"][2] == 2.7
    assert {tuple(x["inicio"][:2]) for x in arr["diagonales"]} == {(-2.0, 0.0), (-2.0, 6.0)}
    # vigas en el nivel superior y columnas de piso a piso
    assert all(v["inicio"][2] == 2.7 for v in mom["vigas"])
    assert all(c["z0"] == 0.0 and c["z1"] == 2.7 for c in mom["columnas"])
    assert any(m["nombre"] == "Acero estructural A270ES" for m in modelo["materiales"])


def test_marcos_lego_por_capa(proyecto):
    py = yaml.safe_load(proyecto.ruta.read_text(encoding="utf-8"))
    py["legos"] = {"marcos": {"S-MARCO-MOMENTO": "MA-1 Marco arriostrado en cruz acero"}}
    modelo = construir_modelo(cargar(proyecto("proyecto.yaml", py)))
    assert all(f["sistema"] == "arriostrado_x" for f in modelo["marcos"] if f["origen"] == "plano CAD")
    norte = next(f for f in modelo["marcos"] if f["capa_cad"] == "S-MARCO-MOMENTO" and f["nivel"] == "N1")
    assert len(norte["diagonales"]) == 4                      # 2 vanos x cruz


def test_marco_invalido(proyecto):
    mat = yaml.safe_load((proyecto.ruta.parent / "materialidades.yaml").read_text(encoding="utf-8"))
    del mat["tipos_marco"][1]["diagonal"]
    with pytest.raises(ErrorConfiguracion, match="falta el perfil de diagonal"):
        cargar(proyecto("materialidades.yaml", mat))


def test_propuesta_por_tramos(proyecto):
    modelo = construir_modelo(cargar(proyecto.ruta))
    assert validar(modelo) == []
    tramos = {t["id"]: t for t in modelo["tramos"]}
    # ejes verticales 1 (x=0.1), 2 (x=6.0, tabique), 3 (x=9.9); horizontales A (y=0.1), B (y=7.9)
    assert set(tramos) == {"1:A-B", "2:A-B", "3:A-B", "A:1-2", "A:2-3", "B:1-2", "B:2-3"}
    assert tramos["1:A-B"]["niveles"]["N1"]["propuesta"] == "muro"
    assert tramos["A:1-2"]["niveles"]["N1"]["propuesta"] == "muro"           # puerta: 15% del tramo
    t2 = tramos["2:A-B"]["niveles"]["N1"]
    assert t2["propuesta"] == "momento" and "vanos" in t2["motivo"]          # tabique con puerta
    desde_tramo = [f for f in modelo["marcos"] if f["origen"] == "tramo 2:A-B"]
    assert len(desde_tramo) == 2 and all(f["sistema"] == "momento" for f in desde_tramo)


def test_eleccion_por_tramo_y_piso(proyecto):
    py = yaml.safe_load(proyecto.ruta.read_text(encoding="utf-8"))
    py["tramos"] = {"2:A-B": "arriostrado", "2:A-B@N2": "ninguno", "A:1-2": "momento"}
    modelo = construir_modelo(cargar(proyecto("proyecto.yaml", py)))
    tramos = {t["id"]: t for t in modelo["tramos"]}
    assert tramos["2:A-B"]["niveles"]["N1"]["final"] == "arriostrado"
    assert tramos["2:A-B"]["niveles"]["N2"]["final"] == "ninguno"
    assert tramos["A:1-2"]["niveles"]["N2"]["final"] == "momento"
    origenes = [(f["origen"], f["nivel"], f["sistema"]) for f in modelo["marcos"] if f["origen"].startswith("tramo")]
    assert ("tramo 2:A-B", "N1", "arriostrado_x") in origenes
    assert not any(o == "tramo 2:A-B" and n == "N2" for o, n, _ in origenes)
    assert sum(1 for o, _, s in origenes if o == "tramo A:1-2" and s == "momento") == 2


def test_ejes_automaticos_desde_muros(proyecto):
    """Plano sin capa de ejes: se generan ejes por los muros y la estructuración funciona igual."""
    py = yaml.safe_load(proyecto.ruta.read_text(encoding="utf-8"))
    py["capas"] = {"ejes": ["CAPA-QUE-NO-EXISTE"]}
    modelo = construir_modelo(cargar(proyecto("proyecto.yaml", py)))
    assert modelo["ejes"] and all(e["origen"] == "desde muros" for e in modelo["ejes"])
    assert {e["nombre"] for e in modelo["ejes"]} == {"1", "2", "3", "A", "B"}
    assert any("ejes automáticos" in a for a in modelo["advertencias"])
    ids = {t["id"] for t in modelo["tramos"]}
    assert {"1:A-B", "2:A-B", "A:1-2", "B:2-3"} <= ids


def test_capas_del_proyecto(proyecto):
    """El proyecto puede decir cuál es su capa de muros: si se omite el tabique, no hay tabique."""
    py = yaml.safe_load(proyecto.ruta.read_text(encoding="utf-8"))
    py["capas"] = {"muros": ["A-MURO-HA"]}
    modelo = construir_modelo(cargar(proyecto("proyecto.yaml", py)))
    assert {m["capa_cad"] for m in modelo["muros"]} == {"A-MURO-HA"}


def test_ejes_partidos_se_unen():
    from cad2revit import ejes as ejes_mod
    from cad2revit.dxf_reader import Segmento
    segs = [Segmento((0, 0), (0, 4), "EJE"), Segmento((0, 4.01), (0, 10), "EJE"),   # eje 1 en 2 trozos
            Segmento((5, 0), (5, 10), "EJE"), Segmento((5.02, 2), (5.02, 6), "EJE"),  # eje 2 + trozo repetido
            Segmento((-1, 3), (8, 3), "EJE")]
    ejes = ejes_mod.nombrar(ejes_mod.desde_cad(segs), {}, "plano CAD")
    assert sorted(e["nombre"] for e in ejes) == ["1", "2", "A"]
    e1 = next(e for e in ejes if e["nombre"] == "1")
    assert e1["inicio"][1] == 0 and e1["fin"][1] == 10


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


def test_planta_y_cuatro_fachadas_es_un_solo_edificio():
    """Planta con puertas + 4 fachadas achuradas (muchas paralelas) y una con 'Cota de Nivel':
    se sugiere UNA planta (la real) y las fachadas no generan estructura."""
    from cad2revit import vistas
    from cad2revit.dxf_reader import DibujoCAD, Insercion, Segmento
    segs, ins = [], []

    def rect(x0, y0, x1, y1, capa):
        segs.extend([Segmento((x0, y0), (x1, y0), capa), Segmento((x1, y0), (x1, y1), capa),
                     Segmento((x1, y1), (x0, y1), capa), Segmento((x0, y1), (x0, y0), capa)])
    # planta 12 x 8 con muros de 20 cm y 3 puertas
    rect(0, 0, 12, 8, "M"); rect(0.2, 0.2, 11.8, 7.8, "M")
    for x in (3, 6, 9):
        ins.append(Insercion("P80", "0Puertas", (x, 0.1), 0, None))
    # 4 fachadas a la derecha, achuradas con líneas cada 10 cm, con línea de suelo
    for k in range(4):
        x0 = 30 + k * 20
        segs.append(Segmento((x0 - 1, 0), (x0 + 13, 0), "F"))
        for j in range(1, 60):
            segs.append(Segmento((x0, j * 0.1), (x0 + 12, j * 0.1), "F"))
    ins.append(Insercion("Cota de Nivel_1", "F", (31, 6.5), 0, None))
    dib = DibujoCAD("x.dxf", 1.0, segs, [], ins, [])
    v = vistas.detectar(dib, es_abertura=lambda i: "Puerta" in i.capa)
    assert len(v) == 5
    plantas = [x for x in v if x["sugerido"] == "planta"]
    assert len(plantas) == 1 and plantas[0]["caja"][0] == 0
    assert sum(x["tipo"] == "fachada o corte" for x in v) == 4
    assert all(x["sugerido"] == "elevacion" for x in v if x is not plantas[0])   # dan alturas, no estructura


def test_achurado_no_cuenta_como_muro():
    from cad2revit.dxf_reader import Segmento
    from cad2revit.muros import largo_pareado_por_capa
    det = {"espesor_min": 0.05, "espesor_max": 0.40, "largo_min": 0.20}
    muro = [Segmento((0, 0), (10, 0), "M"), Segmento((0, 0.2), (10, 0.2), "M")]
    achurado = [Segmento((0, j * 0.1), (10, j * 0.1), "H") for j in range(50)]
    r = largo_pareado_por_capa(muro + achurado, det)
    assert r["M"][0] == 20 and r["H"][0] <= 20      # solo las 2 líneas de borde del achurado


def _dxf_edificio(ruta):
    """Un edificio 10 x 6 m en un solo DXF: planta (con marcas de corte A…A), planta de techo,
    corte A-A (dos aguas: alero 2,5 m, cumbrera 4,0 m) y fachada norte."""
    import ezdxf
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 6
    m = doc.modelspace()
    blk = doc.blocks.new("P80")
    blk.add_line((0, 0), (0.8, 0))

    def rect(x0, y0, x1, y1, capa):
        m.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True, dxfattribs={"layer": capa})

    def txt(t, x, y, capa="TEXTO"):
        m.add_text(t, dxfattribs={"insert": (x, y), "layer": capa, "height": 0.3})
    # planta
    rect(0, 0, 10, 6, "A-MURO"); rect(0.2, 0.2, 9.8, 5.8, "A-MURO")
    m.add_blockref("P80", (4, 0.1), dxfattribs={"layer": "A-PUERTA"})
    txt("A", 5, -1.5); txt("A", 5, 7.5)                       # corte A-A: transversal (vertical en planta)
    m.add_line((5, -1.2), (5, 7.2), dxfattribs={"layer": "A-CORTE"})
    txt("PLANTA PRIMER NIVEL", 2, -3)
    # planta de techo (con aleros de 0,5 m) y cumbrera a lo largo
    rect(30, -0.5, 41, 6.5, "A-TECHO"); m.add_line((30, 3), (41, 3), dxfattribs={"layer": "A-TECHO"})
    txt("PLANTA DE TECHO", 32, -3)
    # corte A-A: 6 m de ancho, alero 2,5, cumbrera 4,0
    x0 = 60
    m.add_line((x0 - 1, 0), (x0 + 7, 0), dxfattribs={"layer": "A-CORTE"})
    m.add_line((x0, 0), (x0, 2.5)); m.add_line((x0 + 6, 0), (x0 + 6, 2.5))
    m.add_line((x0 - 0.5, 2.25), (x0 + 3, 4.0)); m.add_line((x0 + 3, 4.0), (x0 + 6.5, 2.25))
    m.add_line((x0, 2.5), (x0 + 6, 2.5))
    txt("NPT ±0.00", x0 + 6.5, 0); txt("CORTE A-A", x0 + 1, -3)
    # fachada norte (lado largo: techo se ve plano a la altura de la cumbrera)
    x0 = 80
    m.add_line((x0 - 1, 0), (x0 + 11, 0))
    for y in (2.5, 4.0):
        m.add_line((x0, y), (x0 + 10, y))
    m.add_line((x0, 0), (x0, 4.0)); m.add_line((x0 + 10, 0), (x0 + 10, 4.0))
    txt("ELEVACION NORTE", x0 + 2, -3)
    doc.saveas(ruta)


def test_cortes_fachadas_y_techo_de_un_edificio(proyecto, tmp_path):
    from cad2revit import vistas
    from cad2revit.dxf_reader import leer
    ruta = tmp_path / "edificio.dxf"
    _dxf_edificio(ruta)
    v = vistas.detectar(leer(str(ruta)), es_abertura=lambda i: i.nombre == "P80", patron_nivel=r"(?i)NPT\s*[±+-]?\s*(\d+[.,]\d+)")
    por_tipo = {x["tipo"]: x for x in v}
    assert set(por_tipo) == {"planta", "planta de techo", "corte A", "fachada"}, por_tipo.keys()
    assert por_tipo["planta"]["sugerido"] == "planta" and por_tipo["planta de techo"]["sugerido"] == "techo"
    assert por_tipo["corte A"]["sugerido"] == "elevacion" and por_tipo["fachada"]["sugerido"] == "elevacion"
    # proyecto con esas vistas
    py = yaml.safe_load(proyecto.ruta.read_text(encoding="utf-8"))
    py["capas"] = {"muros": ["A-MURO"], "puertas": ["A-PUERTA"]}
    py["niveles"] = [{"nombre": "N1", "cota": 0.0}, {"nombre": "Cubierta", "cota": 3.0}]
    py["plantas"] = [{"nivel": "N1", "archivo": str(ruta), "zonas": [por_tipo["planta"]["caja"]]}]
    py["elevaciones"] = [{"archivo": str(ruta), "zona": por_tipo[t]["caja"]} for t in ("corte A", "fachada")]
    py["techo"] = {"archivo": str(ruta), "zona": por_tipo["planta de techo"]["caja"]}
    modelo = construir_modelo(cargar(proyecto("proyecto.yaml", py)))
    # el corte A-A se conecta con su marca en la planta
    corte = next(c for c in modelo["cortes"] if c["letra"] == "A")
    assert corte["linea"] and abs(corte["linea"][0][0] - 5) < 0.01
    assert corte["alero"] == 2.5 and corte["cumbrera"] == 4.0
    # la cubierta va a la altura del alero, a dos aguas, con los aleros de la planta de techo
    assert {n["nombre"]: n["cota"] for n in modelo["niveles"]}["Cubierta"] == 2.5
    cub = modelo["cubierta"]
    assert cub["forma"] == "dos aguas" and abs(cub["pendiente"] - 26.6) < 1.5
    xs = [p[0] for p in cub["contorno"]]
    assert abs(min(xs) + 0.5) < 0.05 and abs(max(xs) - 10.5) < 0.05          # 0,5 m de alero
    lados = [(cub["contorno"][i], cub["contorno"][(i + 1) % len(cub["contorno"])]) for i in cub["bordes_pendiente"]]
    assert len(lados) == 2 and all(abs(a[1] - b[1]) < 0.01 for a, b in lados)   # los bordes largos (paralelos a la cumbrera)
    losa = next(l for l in modelo["losas"] if l["cubierta"])
    assert losa["pendiente"] == cub["pendiente"] and losa["nivel"] == "Cubierta"
    assert len(modelo["muros"]) == 4


def test_puertas_por_arco_y_ventanas_por_rotulo(proyecto, tmp_path):
    """Plano sin bloques (como los de ArchiCAD): la puerta es un arco de abatimiento y la ventana un
    rótulo de medidas frente a un muro dibujado de corrido."""
    import ezdxf
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 6
    m = doc.modelspace()
    L = lambda a, b: m.add_line(a, b, dxfattribs={"layer": "M"})    # noqa: E731
    # muro inferior (20 cm) con vano de puerta entre x=3 y x=3,9
    L((0, 0), (3, 0)); L((0, 0.2), (3, 0.2)); L((3.9, 0), (10, 0)); L((3.9, 0.2), (10, 0.2))
    L((3, 0), (3, 0.2)); L((3.9, 0), (3.9, 0.2))
    # demás muros, de corrido
    L((0, 6), (10, 6)); L((0.2, 5.8), (9.8, 5.8)); L((0, 0), (0, 6)); L((0.2, 0.2), (0.2, 5.8))
    L((10, 0), (10, 6)); L((9.8, 0.2), (9.8, 5.8))
    m.add_arc((3, 0.2), 0.9, 0, 90, dxfattribs={"layer": "PUERTAS"})          # hoja de 90 cm
    m.add_mtext("VP\\PAnt:100\\PAnc:120\\PH:150", dxfattribs={"insert": (6.5, 7.0), "char_height": 0.2})
    ruta = tmp_path / "sin_bloques.dxf"
    doc.saveas(ruta)
    py = yaml.safe_load(proyecto.ruta.read_text(encoding="utf-8"))
    py["capas"] = {"muros": ["M"]}
    py["niveles"] = [{"nombre": "N1", "cota": 0.0}, {"nombre": "Cubierta", "cota": 2.6}]
    py["plantas"] = [{"nivel": "N1", "archivo": str(ruta)}]
    py["elevaciones"] = []
    modelo = construir_modelo(cargar(proyecto("proyecto.yaml", py)))
    puertas = [a for a in modelo["aberturas"] if a["categoria"] == "puerta"]
    ventanas = [a for a in modelo["aberturas"] if a["categoria"] == "ventana"]
    assert len(puertas) == 1 and abs(puertas[0]["ancho"] - 0.9) < 0.02 and abs(puertas[0]["centro"][0] - 3.45) < 0.05
    assert len(ventanas) == 1
    v = ventanas[0]
    assert (v["ancho"], v["alto"], v["antepecho"]) == (1.2, 1.5, 1.0) and abs(v["centro"][1] - 5.9) < 0.05
    # el muro inferior queda continuo sobre la puerta (un solo muro aloja la puerta)
    assert len([w for w in modelo["muros"] if abs(w["inicio"][1] - 0.1) < 0.02 and abs(w["fin"][1] - 0.1) < 0.02]) == 1


def test_muro_dentro_de_otro_se_descarta():
    from cad2revit.muros import Muro, quitar_encimados
    real = Muro(id="M1", a=(0, 0), b=(10, 0), espesor=0.20, capa_cad="MURO")
    vidrio = Muro(id="M2", a=(2, 0.02), b=(3.2, 0.02), espesor=0.05, capa_cad="VENTANA")    # dentro del muro
    tabique = Muro(id="M3", a=(5, 0.1), b=(5, 4), espesor=0.10, capa_cad="MURO")           # perpendicular: queda
    quedan, quitados = quitar_encimados([real, vidrio, tabique])
    assert [m.id for m in quedan] == ["M1", "M3"] and [m.id for m in quitados] == ["M2"]


def test_por_defecto_solo_vanos(proyecto):
    """Modelo estructural: sin familias de puerta/ventana, solo el hueco exacto en el muro."""
    modelo = construir_modelo(cargar(proyecto.ruta))
    assert modelo["aberturas"] and all(a["modo"] == "hueco" for a in modelo["aberturas"])
    assert not any("necesitan al menos 12.5 cm" in a for a in modelo["advertencias"])
