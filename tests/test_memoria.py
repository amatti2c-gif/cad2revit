"""Memoria de cálculo en Word: capítulos elegidos y numeración."""
import io

from docx import Document

from app.memoria import generar_memoria, opciones_por_defecto
from app.sap.costaneras import ParametrosCostaneras, analizar_costaneras
from tests.test_costaneras import _techo


def _titulos(contenido: bytes) -> list[str]:
    d = Document(io.BytesIO(contenido))
    return [p.text for p in d.paragraphs if p.style.name.startswith("Heading")]


def test_memoria_solo_costaneras():
    techo = {"nombre": "techo", "resultado": analizar_costaneras(_techo(), ParametrosCostaneras(ids=[100, 102]))}
    o = opciones_por_defecto()
    o["datos"] = {"ubicacion": "Castro", "calculista": "Prueba"}
    o["observaciones_extra"] = "Revisar fijaciones"
    t = _titulos(generar_memoria("Proyecto", [], [techo], o))
    assert t[0].startswith("1. Antecedentes")
    assert any("Costaneras de techo" in x for x in t)
    assert not any("acero (AISC" in x or "Fundaciones" in x for x in t)   # sin ejes no hay acero ni fundación
    assert t[-1].endswith("Conclusiones y observaciones")


def test_memoria_quita_capitulos_y_renumera():
    techo = {"nombre": "techo", "resultado": analizar_costaneras(_techo(), ParametrosCostaneras(ids=[100]))}
    o = opciones_por_defecto()
    for k in ("normas", "materiales", "cargas"):
        o["capitulos"][k]["incluir"] = False
    t = _titulos(generar_memoria("Proyecto", [], [techo], o))
    assert t[1] == "2. Combinaciones de carga"
