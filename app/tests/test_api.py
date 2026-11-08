"""Pruebas de la API web con un directorio de datos temporal."""
from __future__ import annotations

import re

import yaml

import base64
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture()
def cliente(tmp_path, monkeypatch):
    monkeypatch.setenv("CAD2REVIT_DATOS", str(tmp_path))
    for v in ("APP_USERNAME", "APP_PASSWORD", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
              "RAILWAY_PROJECT_ID", "CAD2REVIT_EXIGIR_CLAVE"):
        monkeypatch.delenv(v, raising=False)
    import app.servidor as srv
    importlib.reload(srv)
    return TestClient(srv.app)


def _crear(c, ejemplo=True):
    r = c.post("/api/proyectos", json={"nombre": "Casa Ñuñoa", "con_ejemplo": ejemplo})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_flujo_completo(cliente):
    pid = _crear(cliente)
    assert pid == "casa-nunoa"
    assert cliente.get("/").status_code == 200

    planos = cliente.get(f"/api/proyectos/{pid}/planos").json()
    assert {p["nombre"] for p in planos} == {"planta_n1.dxf", "planta_n2.dxf", "elevacion_norte.dxf"}
    assert "A-MURO-HA" in next(p for p in planos if p["nombre"] == "planta_n1.dxf")["capas"]

    ent = cliente.get(f"/api/proyectos/{pid}/entradas").json()
    assert ent["errores"] == []
    assert "../planos/planta_n1.dxf" in ent["archivos"]["proyecto.yaml"]
    assert 'nombre: "Casa Ñuñoa"' in ent["archivos"]["proyecto.yaml"]

    r1 = cliente.post(f"/api/proyectos/{pid}/corridas", json={"notas": "primera"}).json()
    assert r1["ok"] and r1["conteos"]["muros"] == 10 and r1["lod"] == 300

    # Iteración: bajar a LOD 200 -> aberturas pasan a huecos, desaparecen las vigas
    nd = ent["archivos"]["nivel_detalle.yaml"].replace("preset: 300", "preset: 200")
    assert cliente.put(f"/api/proyectos/{pid}/entradas/nivel_detalle.yaml", json={"texto": nd}).json()["errores"] == []
    r2 = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    assert r2["n"] == 2 and r2["lod"] == 200 and r2["conteos"]["vigas"] == 0

    cmp = cliente.get(f"/api/proyectos/{pid}/corridas/1/comparar/2").json()
    assert "nivel_detalle.yaml" in cmp["diferencias"]
    assert cmp["conteos"]["vigas"] == [2, 0]

    det = cliente.get(f"/api/proyectos/{pid}/corridas/2").json()
    assert det["modelo"]["aberturas"][0]["modo"] == "hueco"
    assert det["plantas"] == {"N1": "planta_n1.dxf", "N2": "planta_n2.dxf"}

    assert cliente.put(f"/api/proyectos/{pid}/corridas/2/notas", json={"notas": "ok huecos"}).json()["notas"] == "ok huecos"
    assert cliente.get(f"/api/proyectos/{pid}/corridas/1/modelo.json").status_code == 200

    # Restaurar lineamientos de la corrida 1
    cliente.post(f"/api/proyectos/{pid}/corridas/1/restaurar")
    assert "preset: 300" in cliente.get(f"/api/proyectos/{pid}/entradas").json()["archivos"]["nivel_detalle.yaml"]

    geo = cliente.get(f"/api/proyectos/{pid}/planos/planta_n1.dxf/geometria").json()
    assert len(geo["segmentos"]) > 20

    assert [c["n"] for c in cliente.get(f"/api/proyectos/{pid}/corridas").json()] == [2, 1]


def test_lineamientos_comunes(cliente):
    a = _crear(cliente)
    r = cliente.post("/api/proyectos", json={"nombre": "Otro", "con_ejemplo": True}).json()["id"]
    texto = cliente.get(f"/api/proyectos/{a}/entradas").json()["archivos"]["nivel_detalle.yaml"]
    cliente.put(f"/api/proyectos/{a}/entradas/nivel_detalle.yaml", json={"texto": texto.replace("preset: 300", "preset: 200")})
    # el cambio hecho desde un proyecto vale para todos
    assert "preset: 200" in cliente.get(f"/api/proyectos/{r}/entradas").json()["archivos"]["nivel_detalle.yaml"]
    assert cliente.post(f"/api/proyectos/{r}/corridas", json={}).json()["lod"] == 200
    # proyecto.yaml es propio de cada proyecto
    py = cliente.get(f"/api/proyectos/{a}/entradas").json()["archivos"]["proyecto.yaml"]
    assert "lineamientos/materialidades.yaml" in py


def test_legos(cliente):
    pid = _crear(cliente)
    cat = cliente.get("/api/catalogo").json()
    nombres = [m["nombre"] for m in cat["muros"]]
    madera = next(m for m in cat["muros"] if m["entramado"])
    assert madera["clase_entramado"] == "Madera" and madera["espesor_total"] == 0.106
    lg = cliente.get(f"/api/proyectos/{pid}/legos").json()
    assert lg["capas_muro"] == ["A-MURO-HA", "A-TABIQUE"]
    metalcon = next(n for n in nombres if n.startswith("TB-10"))
    r = cliente.put(f"/api/proyectos/{pid}/legos", json={"muros": {"A-TABIQUE": metalcon}}).json()
    assert r["errores"] == []
    assert cliente.get(f"/api/proyectos/{pid}/legos").json()["muros"] == {"A-TABIQUE": metalcon}
    corrida = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    assert corrida["ok"] and corrida["conteos"]["piezas_entramado"] == 0
    # volver a madera: se reemplaza el bloque, no se duplica
    cliente.put(f"/api/proyectos/{pid}/legos", json={"muros": {"A-TABIQUE": madera["nombre"]}})
    py = cliente.get(f"/api/proyectos/{pid}/entradas").json()["archivos"]["proyecto.yaml"]
    assert py.count("legos:") == 1 and py.count("# Legos elegidos") == 1
    assert cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()["conteos"]["piezas_entramado"] > 0


def test_editor_de_legos(cliente):
    pid = _crear(cliente)
    cat = cliente.get("/api/catalogo").json()
    madera = next(m for m in cat["muros"] if m["entramado"])
    # duplicar con otra sección (2x4) y separación 60
    nuevo = {"nombre": "TM-12 Tabique madera 2x4", "estructural": False,
             "capas": [{"funcion": c["funcion"], "material": c["material"], "espesor": c["espesor"]} for c in madera["capas"]],
             "entramado": dict(madera["entramado"], pie_derecho={"b": 0.05, "h": 0.1}, separacion_pies_derechos=0.6)}
    nuevo["capas"][1]["espesor"] = 0.1
    assert cliente.post("/api/catalogo/muros", json=nuevo).status_code == 201
    assert cliente.post("/api/catalogo/muros", json=nuevo).status_code == 409          # nombre repetido
    mat_txt = cliente.get(f"/api/proyectos/{pid}/entradas").json()["archivos"]["materialidades.yaml"]
    assert "TM-12 Tabique madera 2x4" in mat_txt and "# ====" in mat_txt               # conserva comentarios
    # usarlo en el proyecto, renombrarlo y comprobar que el proyecto sigue la referencia
    cliente.put(f"/api/proyectos/{pid}/legos", json={"muros": {"A-TABIQUE": "TM-12 Tabique madera 2x4"}})
    nuevo2 = dict(nuevo, nombre="TM-12b Tabique 2x4")
    assert cliente.put("/api/catalogo/muros/TM-12 Tabique madera 2x4", json=nuevo2).status_code == 200
    assert cliente.get(f"/api/proyectos/{pid}/legos").json()["muros"]["A-TABIQUE"] == "TM-12b Tabique 2x4"
    corrida = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    assert corrida["ok"], corrida
    # no se puede borrar mientras se usa
    r = cliente.delete("/api/catalogo/muros/TM-12b Tabique 2x4")
    assert r.status_code == 409 and pid in r.json()["detail"]
    # lego inválido (material inexistente) se rechaza sin tocar el archivo
    malo = dict(nuevo, nombre="Malo", capas=[{"funcion": "estructura", "material": "Kriptonita", "espesor": 0.1}])
    r = cliente.post("/api/catalogo/muros", json=malo)
    assert r.status_code == 400 and "Kriptonita" in r.json()["detail"]
    # material nuevo
    assert cliente.post("/api/materiales", json={"nombre": "OSB 11 mm", "clase": "Madera", "color": [200, 160, 100]}).status_code == 201
    assert any(m["nombre"] == "OSB 11 mm" for m in cliente.get("/api/materiales").json())


def test_proyecto_yaml_protegido_y_reparado(cliente, tmp_path):
    pid = _crear(cliente)
    # no se puede guardar sin niveles ni vacío
    assert cliente.put(f"/api/proyectos/{pid}/entradas/proyecto.yaml", json={"texto": ""}).status_code == 400
    assert cliente.put(f"/api/proyectos/{pid}/entradas/materialidades.yaml", json={"texto": ""}).status_code == 400
    # un archivo dañado (como el reportado) se repara solo y conserva los legos
    py = tmp_path / "proyectos" / pid / "entradas" / "proyecto.yaml"
    py.write_text("materialidades: ../../../lineamientos/materialidades.yaml\n\n"
                  "legos:\n  muros:\n    A-TABIQUE: TB-10 Tabique metalcon 10\n", encoding="utf-8")
    texto = cliente.get(f"/api/proyectos/{pid}/entradas").json()["archivos"]["proyecto.yaml"]
    assert "niveles:" in texto and "A-TABIQUE: TB-10 Tabique metalcon 10" in texto
    assert cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()["ok"]


def test_marcos_api(cliente):
    pid = _crear(cliente)
    cat = cliente.get("/api/catalogo").json()
    assert {m["sistema"] for m in cat["marcos"]} == {"momento", "arriostrado_x"}
    lg = cliente.get(f"/api/proyectos/{pid}/legos").json()
    assert lg["capas_marco"] == ["S-MARCO-ARRIOSTRADO", "S-MARCO-MOMENTO"]
    # nuevo lego: marco arriostrado con diagonales de tubo
    nuevo = {"nombre": "MA-2 Arriostrado tubo", "sistema": "arriostrado_x", "material": "Acero estructural A270ES",
             "vano_maximo": 5,
             "columna": {"perfil": "W8X31", "forma": "W", "b": 0.2, "h": 0.2, "familia": "W-Wide Flange-Column"},
             "viga": {"perfil": "W310X21", "forma": "W", "b": 0.101, "h": 0.303, "familia": "M_W Shapes"},
             "diagonal": {"perfil": "", "forma": "tubo", "b": 0.1, "h": 0.1, "familia": ""}}
    assert cliente.post("/api/catalogo/marcos", json=nuevo).status_code == 201
    cliente.put(f"/api/proyectos/{pid}/legos", json={"muros": {}, "marcos": {"S-MARCO-MOMENTO": "MA-2 Arriostrado tubo"}})
    assert cliente.get(f"/api/proyectos/{pid}/legos").json()["marcos"] == {"S-MARCO-MOMENTO": "MA-2 Arriostrado tubo"}
    r = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    assert r["ok"] and r["conteos"]["marcos"] == 4         # los dibujados (el tramo 2:A-B es tabiquería: sin marco)
    modelo = cliente.get(f"/api/proyectos/{pid}/corridas/{r['n']}").json()["modelo"]
    norte = next(f for f in modelo["marcos"] if f["capa_cad"] == "S-MARCO-MOMENTO" and f["nivel"] == "N1")
    assert norte["vanos"] == 2 and len(norte["diagonales"]) == 4 and norte["perfiles"]["diagonal"]["forma"] == "tubo"
    # en uso: no se elimina; sin diagonal: inválido
    assert cliente.delete("/api/catalogo/marcos/MA-2 Arriostrado tubo").status_code == 409
    malo = dict(nuevo, nombre="Malo"); malo.pop("diagonal")
    assert cliente.post("/api/catalogo/marcos", json=malo).status_code == 400


def test_tramos_api(cliente):
    pid = _crear(cliente)
    r = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    modelo = cliente.get(f"/api/proyectos/{pid}/corridas/{r['n']}").json()["modelo"]
    assert {t["id"] for t in modelo["tramos"]} >= {"2:A-B", "A:1-2"}
    # cambiar un tramo a arriostrado y otro solo en N2
    assert cliente.put(f"/api/proyectos/{pid}/tramos",
                       json={"elecciones": {"2:A-B": "arriostrado", "A:1-2@N2": "momento", "B:1-2": "auto"}}).json()["errores"] == []
    assert cliente.get(f"/api/proyectos/{pid}/tramos").json()["elecciones"] == {"2:A-B": "arriostrado", "A:1-2@N2": "momento"}
    # convive con la selección de legos sin pisarse
    cliente.put(f"/api/proyectos/{pid}/legos", json={"muros": {"A-TABIQUE": "TB-10 Tabique metalcon 10"}})
    assert cliente.get(f"/api/proyectos/{pid}/tramos").json()["elecciones"]["2:A-B"] == "arriostrado"
    r2 = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    m2 = cliente.get(f"/api/proyectos/{pid}/corridas/{r2['n']}").json()["modelo"]
    t = {x["id"]: x for x in m2["tramos"]}
    assert t["2:A-B"]["niveles"]["N1"]["final"] == "arriostrado"
    assert t["A:1-2"]["niveles"]["N2"]["final"] == "momento" and t["A:1-2"]["niveles"]["N1"]["final"] == "muro"
    assert cliente.put(f"/api/proyectos/{pid}/tramos", json={"elecciones": {"x": "acero"}}).status_code == 400


def test_subir_dwg_con_convertidor(cliente, tmp_path, monkeypatch):
    """Con un convertidor (simulado) el DWG queda como DXF de trabajo + original guardado."""
    import app.servidor as srv
    pid = _crear(cliente, ejemplo=False)
    ejemplo = tmp_path / "fuente"
    from generar_ejemplo import generar
    generar(ejemplo)

    def convertir_falso(origen, destino):
        destino.write_bytes((ejemplo / "planta_n1.dxf").read_bytes())
    monkeypatch.setattr(srv, "_convertir_dwg", convertir_falso)
    r = cliente.post(f"/api/proyectos/{pid}/planos", files=[("archivos", ("Planta Piso 1.dwg", b"AC1032..."))])
    assert r.status_code == 201 and r.json()["guardados"] == ["Planta Piso 1.dxf"]
    planos = cliente.get(f"/api/proyectos/{pid}/planos").json()
    p = next(x for x in planos if x["nombre"] == "Planta Piso 1.dxf")
    assert p["desde_dwg"] == "Planta Piso 1.dwg" and "A-MURO-HA" in p["capas"]
    base = tmp_path / "proyectos" / pid / "planos"
    assert (base / "originales" / "Planta Piso 1.dwg").exists()
    assert list((base / ".cad2revit").glob("Planta Piso 1.dxf.*.geo.json.gz"))      # formato interno
    # borrar el plano borra también el original y el formato interno
    assert cliente.delete(f"/api/proyectos/{pid}/planos/Planta Piso 1.dxf").status_code == 204
    assert not (base / "originales" / "Planta Piso 1.dwg").exists()
    assert not list((base / ".cad2revit").glob("Planta Piso 1.dxf.*"))


def test_subir_planos_propios_quita_ejemplos_y_asigna(cliente, tmp_path):
    pid = _crear(cliente)                      # proyecto con los 3 planos de ejemplo
    fuente = tmp_path / "fuente"
    from generar_ejemplo import generar
    generar(fuente)
    archivos = [("archivos", ("Mi Planta 1.dxf", (fuente / "planta_n1.dxf").read_bytes())),
                ("archivos", ("Mi Planta 2.dxf", (fuente / "planta_n2.dxf").read_bytes())),
                ("archivos", ("Mi Corte.dxf", (fuente / "elevacion_norte.dxf").read_bytes()))]
    r = cliente.post(f"/api/proyectos/{pid}/planos", files=archivos).json()
    assert sorted(r["ejemplos_quitados"]) == sorted(["planta_n1.dxf", "planta_n2.dxf", "elevacion_norte.dxf"])
    assert r["asignados"] == {"Mi Corte.dxf": "elevacion", "Mi Planta 1.dxf": "planta:N1", "Mi Planta 2.dxf": "planta:N2"}
    planos = {p["nombre"]: p for p in cliente.get(f"/api/proyectos/{pid}/planos").json()}
    assert set(planos) == {"Mi Planta 1.dxf", "Mi Planta 2.dxf", "Mi Corte.dxf"}
    r = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    assert r["ok"] and r["conteos"]["muros"] > 0 and not r.get("planos_sin_usar")


def test_distribucion_de_planos(cliente, tmp_path):
    pid = _crear(cliente)
    planos = {p["nombre"]: p for p in cliente.get(f"/api/proyectos/{pid}/planos").json()}
    assert planos["planta_n1.dxf"]["uso"] == "planta:N1" and planos["elevacion_norte.dxf"]["uso"] == "elevacion"
    assert planos["elevacion_norte.dxf"]["parece_elevacion"] and not planos["planta_n1.dxf"]["parece_elevacion"]
    # un plano nuevo subido queda sin usar y la corrida lo avisa
    base = tmp_path / "proyectos" / pid / "planos"
    (base / "mi_planta.dxf").write_bytes((base / "planta_n2.dxf").read_bytes())
    r = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    assert r["planos_sin_usar"] == ["mi_planta.dxf"]
    # asignarlo: un solo nivel con mi planta, sin elevación
    r = cliente.put(f"/api/proyectos/{pid}/distribucion", json={
        "niveles": [{"nombre": "P1", "cota": 0}, {"nombre": "Techo", "cota": 3.1}],
        "usos": {"mi_planta.dxf": "planta:P1", "planta_n1.dxf": "no", "elevacion_norte.dxf": "no"}}).json()
    assert r["errores"] == []
    dist = cliente.get(f"/api/proyectos/{pid}/distribucion").json()
    assert dist["usos"] == {"mi_planta.dxf": "planta:P1"} and [n["nombre"] for n in dist["niveles"]] == ["P1", "Techo"]
    c = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    assert c["ok"] and c["conteos"]["niveles"] == 2 and "mi_planta.dxf" not in c["planos_sin_usar"]
    py = cliente.get(f"/api/proyectos/{pid}/entradas").json()["archivos"]["proyecto.yaml"]
    assert "Una planta por nivel" in py                  # se conservan los comentarios
    # dos plantas para el mismo nivel: error claro
    r = cliente.put(f"/api/proyectos/{pid}/distribucion", json={
        "niveles": [{"nombre": "P1", "cota": 0}], "usos": {"mi_planta.dxf": "planta:P1", "planta_n1.dxf": "planta:P1"}})
    assert r.status_code == 400 and "dos plantas" in r.json()["detail"]


def test_capas_del_plano(cliente):
    pid = _crear(cliente)
    c = cliente.get(f"/api/proyectos/{pid}/capas").json()
    assert "A-TABIQUE" in c["capas_plano"]
    cat = {x["clave"]: x for x in c["categorias"]}
    assert cat["muros"]["usadas"] == ["A-MURO-HA", "A-TABIQUE"] and cat["muros"]["elegidas"] is None
    # por geometría: las capas de muros tienen líneas paralelas; la de recintos no
    sug = {x["capa"] for x in cat["muros"]["sugeridas"]}
    assert "A-MURO-HA" in sug and "A-RECINTO" not in sug and "A-EJES" not in sug
    # elegir solo el hormigón como muro y una capa de ejes que no existe -> ejes desde muros
    cliente.put(f"/api/proyectos/{pid}/capas", json={"categoria": "muros", "capas": ["A-MURO-HA"]})
    cliente.put(f"/api/proyectos/{pid}/capas", json={"categoria": "ejes", "capas": ["A-RECINTO"]})
    cat = {x["clave"]: x for x in cliente.get(f"/api/proyectos/{pid}/capas").json()["categorias"]}
    assert cat["muros"]["usadas"] == ["A-MURO-HA"] and cat["muros"]["elegidas"] == ["A-MURO-HA"]
    r = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    assert r["ok"] and r["conteos"]["muros"] == 8
    # volver a la regla general
    cliente.put(f"/api/proyectos/{pid}/capas", json={"categoria": "muros", "capas": None})
    cliente.put(f"/api/proyectos/{pid}/capas", json={"categoria": "ejes", "capas": []})
    py = cliente.get(f"/api/proyectos/{pid}/entradas").json()["archivos"]["proyecto.yaml"]
    assert "\ncapas:" not in py
    assert cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()["conteos"]["muros"] == 10


def test_errores_de_lineamientos(cliente):
    pid = _crear(cliente)
    r = cliente.put(f"/api/proyectos/{pid}/entradas/materialidades.yaml", json={"texto": "a: [1, 2"})
    assert r.status_code == 400 and "línea" in r.json()["detail"]

    ent = cliente.get(f"/api/proyectos/{pid}/entradas").json()["archivos"]["materialidades.yaml"]
    malo = ent.replace("material: Hormigón armado H30,     espesor: 0.20", "material: Adobe, espesor: 0.20")
    r = cliente.put(f"/api/proyectos/{pid}/entradas/materialidades.yaml", json={"texto": malo}).json()
    assert any("Adobe" in e for e in r["errores"])
    corrida = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    assert not corrida["ok"] and "Adobe" in corrida["error"]

    assert cliente.put(f"/api/proyectos/{pid}/entradas/otro.yaml", json={"texto": "x: 1"}).status_code == 404


def test_planos_y_seguridad(cliente):
    pid = _crear(cliente, ejemplo=False)
    corrida = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    assert not corrida["ok"] and "No se encuentran" in corrida["error"]

    import app.servidor as srv
    if not srv._convertidor_dwg():
        r = cliente.post(f"/api/proyectos/{pid}/planos", files=[("archivos", ("plano.dwg", b"x"))])
        assert r.status_code == 400 and "DXF" in r.json()["detail"]
    assert cliente.post(f"/api/proyectos/{pid}/planos", files=[("archivos", ("plano.pdf", b"x"))]).status_code == 400
    r = cliente.post(f"/api/proyectos/{pid}/planos", files=[("archivos", ("../x.dxf", b"x"))])
    assert r.status_code == 201 and r.json()["guardados"] == ["x.dxf"]   # se descarta la ruta
    assert cliente.get("/api/proyectos/..%2F..%2Fetc/planos").status_code in (400, 404)


def _cliente_con(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("CAD2REVIT_DATOS", str(tmp_path))
    for v in ("APP_USERNAME", "APP_PASSWORD", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
              "RAILWAY_PROJECT_ID", "CAD2REVIT_EXIGIR_CLAVE"):
        monkeypatch.delenv(v, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import app.servidor as srv
    importlib.reload(srv)
    return TestClient(srv.app)


def _basic(usuario, clave):
    return {"Authorization": "Basic " + base64.b64encode(f"{usuario}:{clave}".encode()).decode()}


def test_clave(tmp_path, monkeypatch):
    c = _cliente_con(tmp_path, monkeypatch, APP_USERNAME="arquitecto", APP_PASSWORD="Ñandú-2026")
    assert c.get("/api/proyectos").status_code == 401
    assert c.get("/salud").status_code == 200
    assert c.get("/api/proyectos", headers=_basic("arquitecto", "Ñandú-2026")).status_code == 200
    assert c.get("/api/proyectos", headers=_basic("otro", "Ñandú-2026")).status_code == 401
    assert c.get("/api/proyectos", headers=_basic("arquitecto", "x")).status_code == 401


def test_railway_sin_credenciales_bloquea(tmp_path, monkeypatch):
    c = _cliente_con(tmp_path, monkeypatch, RAILWAY_ENVIRONMENT="production")
    assert c.get("/").status_code == 503
    assert c.get("/salud").status_code == 200


def test_local_sin_credenciales_abierto(tmp_path, monkeypatch):
    assert _cliente_con(tmp_path, monkeypatch).get("/api/proyectos").status_code == 200


def test_pagina_con_scripts_versionados(cliente):
    r = cliente.get("/")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    m = re.search(r'src="v/(\w+)/app.js"', r.text)
    assert m and 'href="v/' + m.group(1) + '/estilos.css"' in r.text
    js = cliente.get(f"/v/{m.group(1)}/app.js")
    assert js.status_code == 200 and "immutable" in js.headers["cache-control"]
    assert cliente.get(f"/v/{m.group(1)}/vista3d.js").status_code == 200        # import relativo
    assert cliente.get("/v/vieja/app.js").headers["cache-control"] == "no-store"
    assert cliente.get("/v/x/..%2Fservidor.py").status_code in (400, 404)


def _plano_combinado(origen_planta, origen_fachada, destino, dx=80.0):
    """Planta y fachada dibujadas lado a lado en el mismo DXF (como suele venir de ArchiCAD)."""
    import ezdxf
    from ezdxf.addons import Importer
    doc = ezdxf.readfile(origen_planta)
    fach = ezdxf.readfile(origen_fachada)
    imp = Importer(fach, doc)
    antes = set(e.dxf.handle for e in doc.modelspace())
    imp.import_modelspace()
    imp.finalize()
    for e in doc.modelspace():
        if e.dxf.handle not in antes:
            e.translate(dx, 0, 0)
    doc.saveas(destino)


def test_plano_con_planta_y_fachada(cliente, tmp_path):
    pid = _crear(cliente)
    base = tmp_path / "proyectos" / pid / "planos"
    _plano_combinado(base / "planta_n1.dxf", base / "elevacion_norte.dxf", tmp_path / "Todo junto.dxf")
    r = cliente.post(f"/api/proyectos/{pid}/planos",
                     files=[("archivos", ("Todo junto.dxf", (tmp_path / "Todo junto.dxf").read_bytes()))]).json()
    assert r["asignados"]["Todo junto.dxf"].startswith("planta:N1")
    plano = cliente.get(f"/api/proyectos/{pid}/planos").json()[0]
    assert plano["uso"] == "vistas" and len(plano["vistas"]) == 2
    v = {x["sugerido"]: x for x in plano["vistas"]}
    assert v["planta"]["uso"] == "planta:N1" and v["elevacion"]["uso"] == "elevacion"
    assert v["planta"]["caja"][2] < v["elevacion"]["caja"][0]          # la fachada está a la derecha
    r1 = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    assert r1["ok"] and not r1.get("planos_sin_usar")
    muros_vista = r1["conteos"]["muros"]
    # usar el plano completo como planta: la fachada agrega "muros" falsos
    d = cliente.get(f"/api/proyectos/{pid}/distribucion").json()
    cliente.put(f"/api/proyectos/{pid}/distribucion", json={"niveles": d["niveles"], "usos": {"Todo junto.dxf": "planta:N1"}})
    # …pero un plano con varias vistas es UN edificio: la app vuelve a dejar solo la planta
    assert cliente.get(f"/api/proyectos/{pid}/planos").json()[0]["uso"] == "vistas"
    r2 = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    assert r2["conteos"]["muros"] == muros_vista
    # volver a elegir por vista desde la app
    k_pl, k_el = v["planta"]["k"], v["elevacion"]["k"]
    r = cliente.put(f"/api/proyectos/{pid}/distribucion", json={"niveles": d["niveles"], "usos": {
        f"Todo junto.dxf#{k_pl}": "planta:N1", f"Todo junto.dxf#{k_el}": "no"}})
    assert r.status_code == 200, r.text
    vistas = {x["k"]: x["uso"] for x in cliente.get(f"/api/proyectos/{pid}/planos").json()[0]["vistas"]}
    assert vistas == {k_pl: "planta:N1", k_el: "no"}
    assert cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()["conteos"]["muros"] == muros_vista


def test_descargar_plano(cliente):
    pid = _crear(cliente)
    r = cliente.get(f"/api/proyectos/{pid}/planos/planta_n1.dxf/archivo")
    assert r.status_code == 200 and r.content.startswith(b"  0") and "planta_n1.dxf" in r.headers["content-disposition"]
    assert cliente.get(f"/api/proyectos/{pid}/planos/no.dxf/archivo").status_code == 404


def test_vistas_obsoletas_se_reasignan(cliente, tmp_path):
    pid = _crear(cliente)
    base = tmp_path / "proyectos" / pid / "planos"
    _plano_combinado(base / "planta_n1.dxf", base / "elevacion_norte.dxf", tmp_path / "Todo junto.dxf")
    cliente.post(f"/api/proyectos/{pid}/planos",
                 files=[("archivos", ("Todo junto.dxf", (tmp_path / "Todo junto.dxf").read_bytes()))])
    muros_bien = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()["conteos"]["muros"]
    # simular lo que dejó una versión anterior: planta = una caja que junta planta y fachada, marcada como revisada
    py = tmp_path / "proyectos" / pid / "entradas" / "proyecto.yaml"
    datos = yaml.safe_load(py.read_text(encoding="utf-8"))
    datos["plantas"] = [{"nivel": "N1", "archivo": "../planos/Todo junto.dxf", "zonas": [[-5, -5, 100, 20]]}]
    datos["elevaciones"], datos["techo"], datos["vistas_revisadas"] = [], None, ["Todo junto.dxf"]
    py.write_text(yaml.safe_dump(datos, allow_unicode=True, sort_keys=False), encoding="utf-8")
    r = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    assert r["conteos"]["muros"] == muros_bien
    modelo = cliente.get(f"/api/proyectos/{pid}/corridas/{r['n']}").json()["modelo"]
    assert any("versión anterior" in a for a in modelo["advertencias"])
    vistas = cliente.get(f"/api/proyectos/{pid}/planos").json()[0]["vistas"]
    assert sorted(v["uso"] for v in vistas) == ["elevacion", "planta:N1"]


def test_lineamientos_antiguos_se_completan(tmp_path, monkeypatch):
    """Lineamientos copiados antes de que existieran los legos de marco: se agregan sin tocar lo editado."""
    import yaml as _y
    import app.servidor as srv
    datos = tmp_path / "datos"
    lin = datos / "lineamientos"
    lin.mkdir(parents=True)
    for a in srv.LINEAMIENTOS:
        (lin / a).write_text((srv.RAIZ / "entradas" / a).read_text(encoding="utf-8"), encoding="utf-8")
    mat = _y.safe_load((lin / "materialidades.yaml").read_text(encoding="utf-8"))
    for k in ("tipos_marco", "reglas_marco", "tipo_marco_por_defecto"):
        mat.pop(k, None)
    mat["materiales"] = [m for m in mat["materiales"] if "Acero" not in m["nombre"]]
    mat["tipos_muro"][0]["nombre_editado_por_usuario"] = True
    (lin / "materialidades.yaml").write_text(_y.safe_dump(mat, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("CAD2REVIT_DATOS", str(datos))
    srv._COMPLETADOS.clear()
    srv._lineamientos()
    nuevo = _y.safe_load((lin / "materialidades.yaml").read_text(encoding="utf-8"))
    assert {t["nombre"].split()[0] for t in nuevo["tipos_marco"]} >= {"MM-1", "MA-1"}
    assert any("Acero" in m["nombre"] for m in nuevo["materiales"])
    assert nuevo["tipos_muro"][0]["nombre_editado_por_usuario"] is True
    from cad2revit.config import _validar_materialidades
    assert _validar_materialidades(nuevo) == []


def test_ediciones_api(cliente):
    pid = _crear(cliente)
    r = cliente.put(f"/api/proyectos/{pid}/ediciones", json={
        "ejes": {"3": None, "C": {"inicio": [-1, 4], "fin": [11, 4]}},
        "muros": [{"nivel": "N1", "inicio": [0.1, 4], "fin": [6, 4], "tipo": "TB-10 Tabique metalcon 10", "estructural": False}]})
    assert r.status_code == 200 and r.json()["errores"] == []
    assert cliente.get(f"/api/proyectos/{pid}/ediciones").json()["ejes"]["3"] is None
    c = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    m = cliente.get(f"/api/proyectos/{pid}/corridas/{c['n']}").json()["modelo"]
    assert "C" in {e["nombre"] for e in m["ejes"]} and "3" not in {e["nombre"] for e in m["ejes"]}
    assert any(w["id"] == "N1-D001" for w in m["muros"])
    # convive con los tramos y se puede vaciar
    cliente.put(f"/api/proyectos/{pid}/tramos", json={"elecciones": {"C:1-2": "tabique"}})
    assert cliente.get(f"/api/proyectos/{pid}/ediciones").json()["muros"]
    cliente.put(f"/api/proyectos/{pid}/ediciones", json={"ejes": {}, "muros": []})
    assert cliente.get(f"/api/proyectos/{pid}/ediciones").json() == {"ejes": {}, "muros": [], "quitar": []}
    assert cliente.put(f"/api/proyectos/{pid}/ediciones", json={"muros": [{"nivel": "N9", "inicio": [0, 0], "fin": [1, 0]}]}).status_code == 400


def test_quitar_muro_del_plano(cliente):
    pid = _crear(cliente)
    c = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    m = cliente.get(f"/api/proyectos/{pid}/corridas/{c['n']}").json()["modelo"]
    tab = next(w for w in m["muros"] if w["nivel"] == "N1" and w["capa_cad"] == "A-TABIQUE")
    n_ab = sum(1 for a in m["aberturas"] if a["muro"] == tab["id"])
    r = cliente.put(f"/api/proyectos/{pid}/ediciones", json={"quitar": [
        {"nivel": "N1", "inicio": tab["inicio"], "fin": tab["fin"], "id": tab["id"]}]})
    assert r.status_code == 200
    c2 = cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()
    m2 = cliente.get(f"/api/proyectos/{pid}/corridas/{c2['n']}").json()["modelo"]
    assert len(m2["muros"]) == len(m["muros"]) - 1 and not any(w["id"] == tab["id"] for w in m2["muros"])
    assert len(m2["aberturas"]) == len(m["aberturas"]) - n_ab            # sus vanos se van con él
    assert any("Muros quitados" in a for a in m2["advertencias"])


def test_proyecto_danado_se_recupera_de_la_ultima_corrida(cliente, tmp_path):
    pid = _crear(cliente)
    cliente.put(f"/api/proyectos/{pid}/tramos", json={"elecciones": {"2:A-B": "arriostrado"}})
    assert cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()["ok"]
    d = tmp_path / "proyectos" / pid
    py = d / "entradas" / "proyecto.yaml"
    bueno = py.read_text(encoding="utf-8")
    # 1) archivo vacío (leído justo mientras otra consulta lo escribía): se recupera de la corrida, no de la plantilla
    py.write_text("", encoding="utf-8")
    assert cliente.get(f"/api/proyectos/{pid}/tramos").json()["elecciones"] == {"2:A-B": "arriostrado"}
    assert py.read_text(encoding="utf-8") == bueno
    # 2) proyecto que una versión anterior «reparó» con la plantilla (planos de ejemplo que no existen)
    plantilla = bueno.replace("../planos/planta_n1.dxf", "../planos/no_existe_n1.dxf")
    py.write_text(plantilla, encoding="utf-8")
    (d / "reparado.txt").write_text("x", encoding="utf-8")
    assert cliente.get(f"/api/proyectos/{pid}/planos").status_code == 200
    assert "no_existe_n1.dxf" not in py.read_text(encoding="utf-8")
    assert cliente.post(f"/api/proyectos/{pid}/corridas", json={}).json()["ok"]
    assert list((d / "entradas").glob("proyecto.dañado-*.yaml"))
