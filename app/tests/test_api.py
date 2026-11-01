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

    r = cliente.post(f"/api/proyectos/{pid}/planos", files=[("archivos", ("plano.dwg", b"x"))])
    assert r.status_code == 400 and "DXF" in r.json()["detail"]
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
