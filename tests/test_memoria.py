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


def test_figuras_en_su_capitulo():
    from app import figuras as F
    datos = _techo()
    res = analizar_costaneras(datos, ParametrosCostaneras(ids=[100, 102]))
    planta, momentos = F.planta_costaneras(datos, res), F.momentos_costanera(res)
    assert planta[:8] == b"\x89PNG\r\n\x1a\n" and momentos[:8] == b"\x89PNG\r\n\x1a\n"
    corte = F.corte_fundacion({"b_pedestal": 0.2, "h_pedestal": 0.4, "b_zapata": 0.6, "h_zapata": 0.25})
    o = opciones_por_defecto()
    o["capitulos"]["normas"]["incluir"] = False
    figs = [{"png": planta, "leyenda": "Planta", "capitulo": "costaneras"},
            {"png": corte, "leyenda": "Corte", "capitulo": "normas"}]          # capítulo quitado → figuras complementarias
    d = Document(io.BytesIO(generar_memoria("P", [], [{"nombre": "t", "resultado": res}], o, None, figs)))
    textos = [p.text for p in d.paragraphs]
    i_cos = next(i for i, t in enumerate(textos) if "Costaneras de techo" in t and t[0].isdigit())
    i_fig = textos.index("Figura 1: Planta")
    assert i_fig > i_cos
    assert any("Figuras complementarias" in t for t in textos) and "Figura 2: Corte" in textos
    assert len(d.inline_shapes) == 2


def test_version_final_sin_observaciones_pendientes():
    res = analizar_costaneras(_techo(), ParametrosCostaneras(ids=[100], apoyos_editados={"100": {"agregar": [2.5]}}))
    techo = [{"nombre": "t", "resultado": res}]
    texto = lambda o: " ".join(p.text for p in Document(io.BytesIO(generar_memoria("P", [], techo, o))).paragraphs)
    o = opciones_por_defecto()
    o["observaciones_extra"] = "Nota propia"
    borrador = texto(o)
    assert "Antes de emitir la memoria definitiva" in borrador and "cambiados a mano" in borrador
    o["final"] = True
    final = texto(o)
    assert "Antes de emitir" not in final and "cambiados a mano" not in final and "No se verific" not in final
    assert "Nota propia" in final and "Notas:" in final
