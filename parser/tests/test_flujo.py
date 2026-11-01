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
    assert tabique["tipo"].startswith("TB-10") and not tabique["estructural"]
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
