"""Pruebas de la API web con un directorio de datos temporal."""
from __future__ import annotations

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
    assert r["ok"] and r["conteos"]["marcos"] == 6         # 4 dibujados + 2 propuestos por el tramo 2:A-B
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
