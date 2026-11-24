"""Memoria de cálculo (Word) armada con los resultados guardados en la app: ejes (análisis y diseño), fundaciones y
costaneras. Los capítulos y sus detalles se eligen con `opciones["capitulos"]`; la numeración se ajusta sola.

Fuentes por eje: el .sap.json (geometría), sus ajustes (parámetros de cargas, muros, diseño y fundación), el
.resultado.json (análisis PyNite y diseño) y el .fundacion.json. Por techo: el .resultado.json de costaneras.
"""

from __future__ import annotations

import io
import math
from datetime import date

from docx import Document
from docx.enum.section import WD_ORIENT  # noqa: F401  (A4 vertical)
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from app.sap.eje_cargas import A0_ZONA, SISTEMAS, SUELOS_DS61, cmax

AZUL = RGBColor(0x1F, 0x4E, 0x79)
AZUL2 = RGBColor(0x2E, 0x5C, 0x8A)
GRIS = RGBColor(0x55, 0x55, 0x55)
ROJO = RGBColor(0xC0, 0x00, 0x00)
VERDE = RGBColor(0x1B, 0x6E, 0x20)
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre",
         "diciembre"]

# capítulos y detalles disponibles (la interfaz arma sus casillas con esto)
CAPITULOS = [
    ("antecedentes", "Antecedentes generales", [("tabla_eje", "Tabla de datos de cada eje")]),
    ("normas", "Normas y referencias", []),
    ("materiales", "Materiales", []),
    ("cargas", "Cargas", [("D", "Cargas permanentes (D)"), ("Lr", "Sobrecarga de techo (Lr)"), ("W", "Viento (W)"),
                          ("E", "Sismo (E): parámetros NCh433")]),
    ("combinaciones", "Combinaciones de carga", []),
    ("modelo", "Modelo y análisis", [("reacciones", "Reacciones basales por estado de carga"),
                                     ("modos", "Periodos y masa participante")]),
    ("acero", "Diseño de elementos de acero", [("sismicos", "Requisitos sísmicos (columna fuerte–viga débil, Ω0)")]),
    ("madera", "Muros de madera de marco plataforma", [("anclajes", "Tracción de anclaje de los bordes")]),
    ("deformaciones", "Deformaciones", [("todas", "Todas las deformaciones (si no, solo la más exigida de cada tipo)")]),
    ("fundacion", "Fundaciones", [("armaduras", "Geometría y armaduras"), ("suelo", "Suelo (servicio)"),
                                  ("hormigon", "Hormigón armado (ACI 318-19)")]),
    ("costaneras", "Costaneras de techo", [("propiedades", "Propiedades del perfil"), ("tabla", "Tabla de costaneras verificadas"),
                                           ("detalle", "Detalle de la costanera más solicitada")]),
    ("conclusiones", "Conclusiones y observaciones", [("automaticas", "Observaciones automáticas (datos a revisar)")]),
    ("firma", "Firma del calculista", []),
]

PRESETS = {
    "acero": {"fundacion": False, "costaneras": False},
    "acero_fundaciones": {"fundacion": True, "costaneras": False},
    "completa": {"fundacion": True, "costaneras": True},
    "costaneras": {"acero": False, "madera": False, "deformaciones": False, "fundacion": False, "costaneras": True,
                   "modelo": False, "combinaciones": True},
}


def opciones_por_defecto() -> dict:
    caps = {k: {"incluir": True, **{d: True for d, _ in det}} for k, _, det in CAPITULOS}
    caps["deformaciones"]["todas"] = False
    return {"capitulos": caps, "datos": {}, "alcance": "", "observaciones_extra": ""}


# ---------------------------------------------------------------------------------------------- formato
def f(x, d=2):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    s = f"{x:,.{d}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def _sombra(celda, color):
    tcPr = celda._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), color)
    tcPr.append(shd)


class _Doc:
    def __init__(self):
        self.d = Document()
        s = self.d.sections[0]
        s.page_width, s.page_height = Cm(21), Cm(29.7)
        for m in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
            setattr(s, m, Cm(2))
        st = self.d.styles["Normal"]
        st.font.name = "Arial"
        st.font.size = Pt(11)
        st.element.rPr.rFonts.set(qn("w:eastAsia"), "Arial")
        st.paragraph_format.space_after = Pt(5)
        for nombre, tam, color, antes in (("Heading 1", 14, AZUL, 16), ("Heading 2", 12, AZUL2, 11)):
            h = self.d.styles[nombre]
            h.font.name, h.font.size, h.font.bold, h.font.color.rgb = "Arial", Pt(tam), True, color
            h.paragraph_format.space_before, h.paragraph_format.space_after = Pt(antes), Pt(6)
            h.paragraph_format.keep_with_next = True
        self.n1 = 0
        self.n2 = 0

    # figuras pendientes por capítulo: se ponen al final de su capítulo, numeradas
    def figura(self, png: bytes, leyenda: str):
        self.nfig = getattr(self, "nfig", 0) + 1
        par = self.d.add_paragraph()
        par.alignment = WD_ALIGN_PARAGRAPH.CENTER
        par.paragraph_format.keep_with_next = True
        par.paragraph_format.space_before = Pt(6)
        from PIL import Image
        try:
            w, h = Image.open(io.BytesIO(png)).size
        except Exception:
            w, h = 4, 3
        ancho = 16.0 if w / max(h, 1) >= 1.2 else 11.0
        par.add_run().add_picture(io.BytesIO(png), width=Cm(ancho))
        cap = self.p((f"Figura {self.nfig}: ",), leyenda or "", tam=9, color=GRIS, alinear=WD_ALIGN_PARAGRAPH.CENTER, despues=8)
        return cap

    def vaciar(self, clave):
        for png, ley in (getattr(self, "pendientes", {}) or {}).pop(clave, []):
            self.figura(png, ley)

    # títulos numerados automáticamente
    def h1(self, t, clave=None):
        self.vaciar(getattr(self, "clave", None))
        if clave == "conclusiones" and getattr(self, "pendientes", None):
            # figuras de capítulos que no entraron: en su propio capítulo, antes de las conclusiones
            resto = [f for v in self.pendientes.values() for f in v]
            self.pendientes = {}
            if resto:
                self.clave = None
                self.n1 += 1
                self.n2 = 0
                p = self.d.add_heading(f"{self.n1}. Figuras complementarias", level=1)
                self._color(p, AZUL)
                for png, ley in resto:
                    self.figura(png, ley)
        self.clave = clave
        self.n1 += 1
        self.n2 = 0
        p = self.d.add_heading(f"{self.n1}. {t}", level=1)
        self._color(p, AZUL)

    def h2(self, t):
        self.n2 += 1
        p = self.d.add_heading(f"{self.n1}.{self.n2} {t}", level=2)
        self._color(p, AZUL2)

    @staticmethod
    def _color(p, c):
        for r in p.runs:
            r.font.color.rgb = c
            r.font.name = "Arial"

    def p(self, *partes, cursiva=False, tam=None, color=None, alinear=None, antes=None, despues=None):
        par = self.d.add_paragraph()
        for x in partes:
            negrita = isinstance(x, tuple)
            r = par.add_run(x[0] if negrita else x)
            r.bold = negrita
            r.italic = cursiva
            if tam:
                r.font.size = Pt(tam)
            if color:
                r.font.color.rgb = color
        if alinear:
            par.alignment = alinear
        if antes is not None:
            par.paragraph_format.space_before = Pt(antes)
        if despues is not None:
            par.paragraph_format.space_after = Pt(despues)
        return par

    def nota(self, t):
        return self.p(t, cursiva=True, tam=9, color=GRIS)

    def vineta(self, *partes):
        par = self.d.add_paragraph(style="List Bullet")
        for x in partes:
            r = par.add_run(x[0] if isinstance(x, tuple) else x)
            r.bold = isinstance(x, tuple)
        par.paragraph_format.space_after = Pt(3)

    def numerado(self, *partes):
        par = self.d.add_paragraph(style="List Number")
        for x in partes:
            r = par.add_run(x[0] if isinstance(x, tuple) else x)
            r.bold = isinstance(x, tuple)
        par.paragraph_format.space_after = Pt(4)

    def tabla(self, cab, filas, anchos):
        tot = sum(anchos)
        ancho = 17.0
        t = self.d.add_table(rows=1, cols=len(cab))
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        t.style = "Table Grid"
        t.autofit = False
        for i, c in enumerate(cab):
            cel = t.rows[0].cells[i]
            cel.text = ""
            r = cel.paragraphs[0].add_run(str(c))
            r.bold, r.font.size, r.font.color.rgb = True, Pt(9), RGBColor(0xFF, 0xFF, 0xFF)
            _sombra(cel, "1F4E79")
        # encabezado repetido en cada página
        trPr = t.rows[0]._tr.get_or_add_trPr()
        th = OxmlElement("w:tblHeader")
        th.set(qn("w:val"), "true")
        trPr.append(th)
        for fila in filas:
            fila_t = t.add_row()
            trp = fila_t._tr.get_or_add_trPr()
            cs = OxmlElement("w:cantSplit")
            cs.set(qn("w:val"), "true")
            trp.append(cs)
            cels = fila_t.cells
            for i, v in enumerate(fila):
                estilo = v if isinstance(v, dict) else {"t": v}
                cels[i].text = ""
                par = cels[i].paragraphs[0]
                r = par.add_run(str(estilo["t"]))
                r.font.size = Pt(9)
                r.bold = estilo.get("bold", False)
                if estilo.get("color"):
                    r.font.color.rgb = estilo["color"]
                if i > 0 and isinstance(estilo["t"], str) and estilo["t"] and all(ch in "−-0123456789.,%°·/× " for ch in estilo["t"]):
                    par.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        for fila in t.rows:
            for i, cel in enumerate(fila.cells):
                cel.width = Cm(ancho * anchos[i] / tot)
                for par in cel.paragraphs:
                    par.paragraph_format.space_after = Pt(0)
        self.d.add_paragraph().paragraph_format.space_after = Pt(2)
        return t

    def salto(self):
        self.d.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    def indice(self):
        p = self.p(("Contenido",), tam=14, color=AZUL)
        par = self.d.add_paragraph()
        r = par.add_run()
        for tipo, texto in (("begin", None), (None, 'TOC \\o "1-2" \\h \\z \\u'), ("separate", None),
                            (None, None), ("end", None)):
            if tipo:
                fc = OxmlElement("w:fldChar")
                fc.set(qn("w:fldCharType"), tipo)
                r._r.append(fc)
            elif texto:
                it = OxmlElement("w:instrText")
                it.set(qn("xml:space"), "preserve")
                it.text = texto
                r._r.append(it)
            else:
                t = OxmlElement("w:t")
                t.text = "Haz clic derecho y «Actualizar campo» para ver el índice."
                r._r.append(t)
        # Word actualiza los campos al abrir
        ajustes = self.d.settings.element
        uf = OxmlElement("w:updateFields")
        uf.set(qn("w:val"), "true")
        ajustes.append(uf)
        return p

    def encabezado(self, izq, der, logo: bytes | None = None):
        s = self.d.sections[0]
        s.different_first_page_header_footer = True
        hp = s.header.paragraphs[0]
        hp.text = ""
        hp.paragraph_format.tab_stops.add_tab_stop(Cm(8.5), WD_TAB_ALIGNMENT.CENTER)
        hp.paragraph_format.tab_stops.add_tab_stop(Cm(17), WD_TAB_ALIGNMENT.RIGHT)
        if logo:
            hp.add_run().add_picture(io.BytesIO(logo), height=Cm(1.1))
        r = hp.add_run(f"\t{izq}\t{der}")
        r.font.size, r.font.color.rgb = Pt(8), GRIS
        _borde_inferior(hp)
        fp = s.footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        for parte in ("Página ", "PAGE", " de ", "NUMPAGES"):
            r = fp.add_run()
            r.font.size, r.font.color.rgb = Pt(8), GRIS
            if parte in ("PAGE", "NUMPAGES"):
                for tipo, texto in (("begin", None), (None, parte), ("end", None)):
                    if tipo:
                        fc = OxmlElement("w:fldChar")
                        fc.set(qn("w:fldCharType"), tipo)
                        r._r.append(fc)
                    else:
                        it = OxmlElement("w:instrText")
                        it.text = texto
                        r._r.append(it)
            else:
                r.text = parte

    def bytes(self):
        b = io.BytesIO()
        self.d.save(b)
        return b.getvalue()


def _borde_inferior(p):
    pPr = p._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    b = OxmlElement("w:bottom")
    for k, v in (("w:val", "single"), ("w:sz", "4"), ("w:space", "4"), ("w:color", "A6A6A6")):
        b.set(qn(k), v)
    bdr.append(b)
    pPr.append(bdr)


def _bordes_celda(celda, ver: bool):
    tcPr = celda._tc.get_or_add_tcPr()
    b = OxmlElement("w:tcBorders")
    for lado in ("top", "left", "bottom", "right"):
        e = OxmlElement(f"w:{lado}")
        e.set(qn("w:val"), "single" if ver else "nil")
        if ver:
            e.set(qn("w:sz"), "6")
            e.set(qn("w:color"), "000000")
        b.append(e)
    tcPr.append(b)


def _texto_celda(celda, texto, tam=8, negrita=False, centro=False, color=None):
    celda.text = ""
    p = celda.paragraphs[0]
    p.paragraph_format.space_after = Pt(0)
    if centro:
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(texto or "")
    r.font.size, r.bold = Pt(tam), negrita
    if color:
        r.font.color.rgb = color
    return p


REVISIONES_DEFECTO = [{"rev": "0", "comentario": "Aprobado Cliente"},
                      {"rev": "B", "ejecutor": "Andrés V.", "comentario": "A Cliente"},
                      {"rev": "A", "comentario": "Coordinación Interna"}]


def _portada(D: "_Doc", datos: dict, titulo: str, empresa: dict, revisiones: list[dict]):
    """Portada con el formato de la oficina: logo y versión arriba, recuadro con el nombre del proyecto, título y tabla
    de revisiones al pie (Ejecutor, Revisor, Aprobador con nombre/firma y fecha, y comentario)."""
    d = D.d
    t = d.add_table(rows=1, cols=2)
    t.autofit = False
    izq, der = t.rows[0].cells
    for c, w in ((izq, 8.5), (der, 8.5)):
        c.width = Cm(w)
        _bordes_celda(c, False)
    if empresa.get("logo"):
        izq.paragraphs[0].add_run().add_picture(io.BytesIO(empresa["logo"]), width=Cm(2.6))
    elif empresa.get("nombre"):
        _texto_celda(izq, empresa["nombre"], tam=12, negrita=True)
    p = _texto_celda(der, f"VERSIÓN {datos.get('version') or 1}", tam=10, negrita=True, color=AZUL)
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    D.p("", antes=24)
    caja = d.add_table(rows=1, cols=1)
    caja.style = "Table Grid"
    caja.alignment = WD_TABLE_ALIGNMENT.CENTER
    cel = caja.rows[0].cells[0]
    cel.width = Cm(14.5)
    trPr = caja.rows[0]._tr.get_or_add_trPr()
    alto = OxmlElement("w:trHeight")
    alto.set(qn("w:val"), "1474")
    alto.set(qn("w:hRule"), "atLeast")
    trPr.append(alto)
    tcPr = cel._tc.get_or_add_tcPr()
    va = OxmlElement("w:vAlign")
    va.set(qn("w:val"), "center")
    tcPr.append(va)
    _texto_celda(cel, titulo.upper(), tam=12, negrita=True, centro=True)
    if datos.get("ubicacion"):
        q = cel.add_paragraph()
        q.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = q.add_run(datos["ubicacion"])
        r.font.size = Pt(10)
    D.p(("MEMORIA DE CÁLCULO ESTRUCTURAL",), tam=20, alinear=WD_ALIGN_PARAGRAPH.CENTER, antes=110, despues=4)
    if datos.get("resumida", True):
        D.p("Resumida", tam=12, color=GRIS, alinear=WD_ALIGN_PARAGRAPH.CENTER, despues=0)
    D.p("", antes=150 if len(revisiones) <= 3 else 110)
    # tabla de revisiones: dos filas por revisión (nombre/firma y fecha) y la fila de títulos al pie
    revs = revisiones or REVISIONES_DEFECTO
    anchos = [1.4, 1.9, 3.0, 3.0, 3.0, 4.7]
    tr = d.add_table(rows=2 * len(revs) + 1, cols=6)
    tr.style = "Table Grid"
    tr.alignment = WD_TABLE_ALIGNMENT.CENTER
    tr.autofit = False
    firma = empresa.get("firma_rev")          # revisión que lleva la firma escaneada del ejecutor (opcional)
    for i, rv in enumerate(revs):
        a, b = tr.rows[2 * i].cells, tr.rows[2 * i + 1].cells
        _texto_celda(a[1], "Nombre / Firma", tam=7, negrita=True, color=AZUL)
        _texto_celda(b[1], "Fecha", tam=7, negrita=True, color=AZUL)
        for j, campo in ((2, "ejecutor"), (3, "revisor"), (4, "aprobador")):
            _texto_celda(a[j], rv.get(campo) or "", tam=8, negrita=True, centro=True)
            _texto_celda(b[j], rv.get(f"fecha_{campo}") or (rv.get("fecha") if rv.get(campo) else "") or "", tam=8, centro=True)
        m = a[0].merge(b[0])
        _texto_celda(m, rv.get("rev", ""), tam=9, negrita=True, centro=True)
        m = a[5].merge(b[5])
        _texto_celda(m, rv.get("comentario", ""), tam=8, negrita=True, centro=True)
        for c in (m, tr.rows[2 * i].cells[0]):
            tcPr = c._tc.get_or_add_tcPr()
            va = OxmlElement("w:vAlign")
            va.set(qn("w:val"), "center")
            tcPr.append(va)
    pie = tr.rows[-1].cells
    for j, txt in enumerate(("REV.", "", "Ejecutor", "Revisor", "Aprobador", "COMENTARIOS")):
        _texto_celda(pie[j], txt, tam=8, negrita=True, centro=True)
    for fila in tr.rows:
        for j, c in enumerate(fila.cells):
            c.width = Cm(anchos[j])


def _razon(r):
    if r is None:
        return "—"
    return {"t": f(r, 2), "bold": True, "color": ROJO if r > 1 else VERDE}


def _combo(c):
    import re
    return re.sub(r"^C\d+b?_", "", c or "—")


def _fecha_texto(iso: str | None) -> str:
    try:
        d = date.fromisoformat((iso or "")[:10])
    except ValueError:
        d = date.today()
    return f"{d.day} de {MESES[d.month - 1]} de {d.year}"


# ---------------------------------------------------------------------------------------------- observaciones
def observaciones(ejes: list[dict], techos: list[dict], caps: dict) -> list[tuple[str, str]]:
    """(título, texto) de los datos a revisar, sacados de los resultados guardados."""
    obs = []
    for e in ejes:
        res, par, geo = e.get("resultado") or {}, (e.get("ajustes") or {}).get("parametros") or {}, e.get("eje") or {}
        nombre = f"Eje {geo.get('eje', e['nombre'])}"
        cg = par.get("cargas") or {}
        sis = res.get("sistema") or {}
        clave_acero = res.get("sistema_acero") or cg.get("sistema_acero")
        tabla = SISTEMAS.get(clave_acero)
        if tabla and tabla[2] and sis.get("R0") and abs(sis["R0"] - tabla[2]) > 1e-6 and sis.get("manda", "").startswith("acero"):
            t0 = SUELOS_DS61.get(cg.get("suelo", "C"), SUELOS_DS61["C"])[1]
            T = res.get("T") or 0.1
            r_act = 1 + T / (0.1 * t0 + T / sis["R0"])
            r_tab = 1 + T / (0.1 * t0 + T / tabla[2])
            obs.append(("R0 del sistema",
                        f"{nombre}: el análisis usa R0 = {f(sis['R0'], 1)} (ingresado en la app), pero la Tabla 5 de NCh433:2026 da "
                        f"R0 = {f(tabla[2], 1)} para {tabla[0]}. Con el valor de la tabla, R* pasa de {f(r_act, 2)} a {f(r_tab, 2)}; los "
                        f"esfuerzos y desplazamientos espectrales cambian en un factor {f(r_act / r_tab, 2)} (el corte de diseño puede "
                        f"quedar limitado por Qmín o Qmáx). Borra el R0 ingresado y vuelve a analizar."))
        lim = (res.get("limites_deformacion") or {}).get("deriva_sismo")
        if lim and sis.get("deriva") and abs(lim - sis["deriva"]) > 1e-9:
            obs.append(("Límite de deriva",
                        f"{nombre}: se usó {f(lim, 4)}·h (ingresado en la app); la Tabla 5 da {f(sis['deriva'], 4)}·h para el sistema"
                        + (" (el ingresado es más exigente)." if lim < sis["deriva"] else ". El valor ingresado es menos exigente que la norma.")))
        import re
        for m in (geo.get("pilares") or []) + (geo.get("vigas") or []):
            nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", m.get("seccion") or "")]
            dims = m.get("dims") or {}
            if len(nums) >= 3 and "d" in dims and "bf" in dims and "t" in dims:
                d_, b_, t_ = dims["d"] * 1000, dims["bf"] * 1000, dims["t"] * 1000
                if abs(nums[0] - d_) > 1 or abs(nums[1] - b_) > 1 or abs(nums[-1] - t_) > 0.2:
                    obs.append(("Sección en Revit",
                                f"{nombre}: el tipo «{m['seccion']}» tiene parámetros {f(d_, 0)}×{f(b_, 0)}×{f(t_, 1)} mm, y el análisis "
                                f"usó los parámetros. Confirma la sección real y corrige el tipo en Revit."))
                    break
        mad = [m for m in res.get("madera") or [] if m.get("falta_capacidad_union")]
        if mad and caps.get("madera", {}).get("incluir"):
            peor = max(mad, key=lambda m: m.get("T_anclaje") or 0)
            obs.append(("Unión muro–pilar",
                        f"{nombre}: falta ingresar la capacidad admisible a tracción de la unión del pie derecho de borde al pilar; la "
                        f"tracción máxima es {f(peor.get('T_anclaje'))} tonf (muro {peor.get('muro')}, {peor.get('combo_anclaje')})."))
        for clave, lista, etiqueta in (("acero", res.get("acero"), "elementos de acero"), ("madera", res.get("madera"), "muros de madera"),
                                       ("deformaciones", res.get("deformaciones"), "deformaciones")):
            if not caps.get(clave, {}).get("incluir"):
                continue
            malos = [x for x in lista or [] if (x.get("razon") if "razon" in x else x.get("FU") or 0) and
                     (x.get("razon") if "razon" in x else x.get("FU")) > 1]
            if malos:
                obs.append((f"No cumplen: {etiqueta}", f"{nombre}: {len(malos)} con razón mayor que 1."))
        fun = e.get("fundacion")
        if fun and caps.get("fundacion", {}).get("incluir"):
            s = fun.get("resumen_suelo") or {}
            if (s.get("FS_desliz_min") or 99) < 1.5:
                obs.append(("Deslizamiento de la fundación",
                            f"{nombre}: FS = {f(s['FS_desliz_min'])} < 1,5. Considera el empuje pasivo del suelo frente al pedestal y la "
                            f"zapata, o la continuidad con las fundaciones transversales; si no basta, aumenta la sección o agrega una "
                            f"llave de corte."))
            if (s.get("pct_compresion_min") or 1) < 0.8:
                obs.append(("Área en compresión", f"{nombre}: {f(s['pct_compresion_min'] * 100, 1)} % < 80 % (NCh433:2026 7.2.1)."))
            if (s.get("FU_presion_max") or 0) > 1:
                obs.append(("Presión de contacto", f"{nombre}: supera la admisible (FU {f(s['FU_presion_max'])})."))
            if s.get("inestables"):
                obs.append(("Estabilidad de la fundación", f"{nombre}: {len(s['inestables'])} combinación(es) sin equilibrio (vuelco)."))
            malos = [v for v in (fun.get("viga") or []) + (fun.get("zapata") or []) + (fun.get("pedestal") or [])
                     if v.get("razon") is not None and v["razon"] > 1 and v.get("minimo") is None]
            if malos:
                obs.append(("Hormigón de la fundación", f"{nombre}: {len(malos)} verificación(es) con razón mayor que 1."))
    if caps.get("costaneras", {}).get("incluir"):
        for t in techos:
            r = t.get("resultado") or {}
            malos = [c for c in r.get("costaneras") or [] if (c.get("FU") or 0) > 1]
            if malos:
                obs.append(("Costaneras que no cumplen", f"{len(malos)} costanera(s) con FU > 1 (máx. {f(max(c['FU'] for c in malos))}): "
                            f"usa «Iterar la sección» en la pestaña 8."))
            editadas = [k for k, v in ((r.get("parametros") or {}).get("apoyos_editados") or {}).items() if v.get("quitar") or v.get("agregar")]
            if editadas:
                obs.append(("Apoyos de costaneras cambiados a mano",
                            f"Costaneras {', '.join(editadas)}: tienen apoyos agregados o quitados en la app; deben corresponder a vigas o "
                            f"soportes reales."))
        if techos:
            obs.append(("Alcance de la verificación de costaneras",
                        "No se verificaron el pandeo distorsional (AISI F4), el aplastamiento del alma en los apoyos (G5) ni las "
                        "conexiones; confirma el perfil con el proveedor."))
    return obs


# ---------------------------------------------------------------------------------------------- memoria
def generar_memoria(proyecto: str, ejes: list[dict], techos: list[dict], opciones: dict, empresa: dict | None = None,
                    figuras: list[dict] | None = None) -> bytes:
    """ejes: [{nombre, eje, ajustes, resultado, fundacion}], techos: [{nombre, resultado}]; empresa: {nombre, logo
    (bytes), firma (bytes)} para la portada, el encabezado y la firma."""
    empresa = empresa or {}
    # datos de la empresa (nombre, calculista, RUT) si la memoria no los trae
    opciones = {**opciones, "datos": {**{k: v for k, v in (("empresa", empresa.get("nombre")), ("calculista", empresa.get("calculista")),
                                                            ("rut", empresa.get("rut"))) if v}, **(opciones.get("datos") or {})}}
    base = opciones_por_defecto()
    caps = base["capitulos"]
    for k, v in (opciones.get("capitulos") or {}).items():
        if k in caps:
            caps[k].update(v if isinstance(v, dict) else {"incluir": bool(v)})
    inc = lambda k, d=None: caps.get(k, {}).get("incluir") and (d is None or caps[k].get(d, True))
    datos = {**base["datos"], **(opciones.get("datos") or {})}
    ejes = [e for e in ejes if e.get("resultado")]
    if not (inc("costaneras") and techos):
        techos = []
    if not ejes:
        for k in ("acero", "madera", "deformaciones", "modelo", "fundacion"):
            caps[k]["incluir"] = False
    if not inc("fundacion"):
        pass
    con_fun = [e for e in ejes if e.get("fundacion")] if inc("fundacion") else []
    nombre_p = datos.get("proyecto") or proyecto
    D = _Doc()
    # figuras elegidas: {capitulo, png, leyenda}; van al final de su capítulo
    D.pendientes = {}
    for fg in figuras or []:
        D.pendientes.setdefault(fg.get("capitulo") or "conclusiones", []).append((fg["png"], fg.get("leyenda") or ""))
    titulo = datos.get("titulo") or nombre_p
    D.encabezado(f"{titulo} · Memoria de cálculo estructural", f"Versión {datos.get('version') or 1}", empresa.get("logo"))
    partes = []
    if ejes:
        partes.append("estructura de acero con muros de madera de marco plataforma")
    if con_fun:
        partes.append("fundación corrida de hormigón armado")
    if techos:
        partes.append("costaneras de acero conformado en frío")
    # ---- portada (formato de la oficina)
    _portada(D, datos, titulo, empresa, opciones.get("revisiones") or REVISIONES_DEFECTO)
    D.salto()
    D.indice()
    D.salto()

    def eje_titulo(e):
        return f"Eje {(e.get('eje') or {}).get('eje', e['nombre'])}"

    # ---- antecedentes
    if inc("antecedentes"):
        D.h1("Antecedentes generales", "antecedentes")
        D.p(f"La presente memoria resume el análisis y diseño estructural del proyecto {nombre_p}"
            + (f", ubicado en {datos['ubicacion']}" if datos.get("ubicacion") else "")
            + ". El modelo se exportó desde Revit y se analizó y diseñó con la aplicación cad2revit (análisis con PyNite).")
        if partes:
            D.p("El alcance de esta memoria comprende:")
            if ejes:
                D.vineta(f"{', '.join(eje_titulo(e) for e in ejes)}: pilares y vigas de acero y muros de madera de marco plataforma "
                         "unidos de forma solidaria a los pilares;")
            if con_fun:
                D.vineta(f"fundación corrida de hormigón armado (pedestal sobre zapata) de {', '.join(eje_titulo(e) for e in con_fun)};")
            if techos:
                D.vineta("costaneras de techo de acero conformado en frío: " + ", ".join(
                    s.get("nombre", "") for t in techos for s in (t["resultado"].get("sistemas") or [])) + ".")
        if opciones.get("alcance"):
            D.p(("Alcance. ",), opciones["alcance"])
        if inc("antecedentes", "tabla_eje"):
            for e in ejes:
                g = e.get("eje") or {}
                filas = [["Eje", f"{g.get('eje')}, largo {f(g.get('largo_eje'))} m; ancho tributario según Revit {f(g.get('ancho_tributario'))} m"],
                         ["Niveles", "; ".join(f"{v['nombre']} ({f(v['z'])} m)" for v in g.get("niveles") or [])],
                         ["Pilares", f"{len(g.get('pilares') or [])}: " + ", ".join(f"{p['seccion']} (h {f(p['z1'] - p['z0'])} m)" for p in g.get("pilares") or [])],
                         ["Vigas", f"{len(g.get('vigas') or [])}: " + ", ".join(sorted({v['seccion'] for v in g.get('vigas') or []}))],
                         ["Muros de madera", f"{len(g.get('muros') or [])}: " + ", ".join(f"{f(m['x1'] - m['x0'])} m" for m in g.get("muros") or [])]]
                D.tabla(["Dato", f"{eje_titulo(e)}"], filas, [25, 75])

    # ---- normas
    if inc("normas"):
        D.h1("Normas y referencias", "normas")
        if ejes or techos:
            D.vineta("NCh3171:2010 Disposiciones generales y combinaciones de cargas.")
            D.vineta("NCh1537:2009 Cargas permanentes y cargas de uso.")
            D.vineta("NCh432:2010 Cálculo de la acción del viento sobre las construcciones.")
        if ejes:
            D.vineta("NCh433:2026 Diseño sísmico de edificios.")
            D.vineta("NCh427/1 (AISC 360-16) Especificación para el cálculo de estructuras de acero; AISC 341-16 (informativo).")
            if inc("madera"):
                D.vineta("NCh1198 y Manual de diseño de estructuras en madera (muros de marco plataforma).")
        if techos:
            D.vineta("NCh427/2 (AISI S100-16) Acero conformado en frío.")
        if con_fun:
            D.vineta("ACI 318-19 Hormigón estructural.")

    # ---- materiales
    if inc("materiales"):
        D.h1("Materiales", "materiales")
        filas = []
        if ejes:
            par = (ejes[0].get("ajustes") or {}).get("parametros") or {}
            mu = par.get("muro") or {}
            filas.append(["Pilares y vigas", "Acero A270ES", f"fy = {f((par.get('diseno') or {}).get('fy_mpa', 270), 0)} MPa; E = 200 GPa"])
            if inc("madera"):
                filas.append(["Muros", "Madera, entramado de pies derechos",
                              f"escuadría de borde {mu.get('escuadria_borde', '2x4')} ({mu.get('n_ppdd_borde', 2)} piezas); "
                              f"{mu.get('tableros', 1)} tablero(s); clavos a {mu.get('espaciamiento_clavos', 100)} mm"
                              + (f"; E = {f(mu['e_ppdd'] * 9.80665e-3, 0)} MPa" if mu.get("e_ppdd") else "")])
        if techos:
            filas.append(["Costaneras", "Acero conformado en frío", f"fy = {f((techos[0]['resultado'].get('parametros') or {}).get('fy', 270), 0)} MPa; E = 203 GPa"])
        if con_fun:
            fp = ((con_fun[0].get("ajustes") or {}).get("parametros") or {}).get("fundacion") or {}
            s = con_fun[0]["fundacion"].get("resumen_suelo") or {}
            filas.append(["Fundación", "Hormigón y acero de refuerzo", f"f'c = {f(fp.get('fc', 25), 0)} MPa; fy = {f(fp.get('fy', 420), 0)} MPa; "
                          f"recubrimiento {f((fp.get('recubrimiento') or 0.05) * 100, 0)} cm"])
            filas.append(["Suelo", "Suelo de fundación", f"q adm = {f(s.get('q_adm'), 1)} tonf/m² (estática), {f(s.get('q_adm_eventual'), 1)} "
                          f"tonf/m² (eventual); ks = {f(fp.get('ks', 3), 1)} kgf/cm³; μ = {f(fp.get('roce', 0.4))}"])
        D.tabla(["Elemento", "Material", "Propiedades"], filas, [20, 28, 52])

    # ---- cargas
    if inc("cargas") and (ejes or techos):
        D.h1("Cargas", "cargas")
        if ejes:
            e = ejes[0]
            par = (e.get("ajustes") or {}).get("parametros") or {}
            cg, mu = par.get("cargas") or {}, par.get("muro") or {}
            res = e["resultado"]
            if inc("cargas", "D"):
                D.h2("Cargas permanentes (D)")
                D.p(f"Peso propio de los perfiles de acero y de los muros ({f((mu.get('peso') or 0.05) * 1000, 0)} kgf/m² de muro). Peso del techo "
                    f"{f((cg.get('peso_techo') or 0.03) * 1000, 0)} kgf/m² (cubierta, costaneras y aislación)"
                    + (f", con un ancho colaborante de {f(cg['ancho_colaborante'])} m." if cg.get("ancho_colaborante") else
                       ", con el ancho tributario de cada eje."))
            if inc("cargas", "Lr"):
                D.h2("Sobrecarga de techo (Lr), NCh1537")
                D.p(f"Lo = {f((cg.get('lo_techo') or 0.102) * 1000, 0)} kgf/m² (techo accesible solo para mantención)"
                    + (", reducida por área tributaria y pendiente (§8.2), con el mínimo de 0,3 kPa en proyección horizontal." if cg.get("reducir_lr", True) else ", sin reducción."))
            if inc("cargas", "W"):
                D.h2("Viento (W), NCh432:2010")
                D.p(f"V = {f(cg.get('v_viento', 40), 0)} m/s, exposición {cg.get('exposicion', 'C')}, Kzt = {f(cg.get('kzt', 1), 1)}, Kd = 0,85, "
                    f"G = 0,85, GCpi = ±{f(cg.get('gcpi', 0.18))} (edificio cerrado). En los ejes, ocho casos (W1 a W8): viento en +X y −X, "
                    "los dos valores de Cp del techo (Tabla 15) y ±GCpi; muros con Cp = +0,8 a barlovento y el de sotavento según L/B."
                    + (" En las costaneras, coeficientes de componentes y revestimientos según la zona y el área efectiva." if techos else ""))
            if inc("cargas", "E"):
                D.h2("Sismo (E), NCh433:2026")
                sis = res.get("sistema") or {}
                suelo = cg.get("suelo", "C")
                S, T0, Tp, nS, pS = SUELOS_DS61.get(suelo, SUELOS_DS61["C"])
                zona = int(cg.get("zona", 3))
                for e2 in ejes:
                    r2 = e2["resultado"]
                    if len(ejes) > 1:
                        D.p((eje_titulo(e2),))
                    D.tabla(["Parámetro", "Valor"], [
                        ["Zona / A0", f"Zona {zona} / A0 = {f(A0_ZONA.get(zona, 0.4))} g (Tabla 7)"],
                        ["Suelo", f"Tipo {suelo}: S = {f(S)}, T0 = {f(T0)} s, T' = {f(Tp)} s, n = {f(nS)}, p = {f(pS, 1)} (Tabla 8)"],
                        ["Importancia", f"I = {f(cg.get('importancia_sismo', 1), 1)} (Tabla 6)"],
                        ["Sistema", (r2.get("sistema") or {}).get("texto", "—")],
                        ["Periodo fundamental T*", f"{f(r2.get('T'), 3)} s"],
                        ["R*", f(r2.get("R_estrella"), 3)],
                        ["Peso sísmico P", f"{f(r2.get('peso_sismico'), 3)} tonf"],
                        ["Qmín / Qmáx", f"{f(r2.get('q_min'), 3)} / {f(r2.get('q_max'), 3)} tonf (Cmáx = {f(cmax((r2.get('sistema') or {}).get('R', 3.5)), 3)}·S·A0/g)"],
                        ["Corte basal espectral → de diseño", f"{f(r2.get('corte_basal_espectral'), 3)} → {f(r2.get('corte_basal'), 3)} tonf "
                         f"({f((r2.get('corte_basal') or 0) / max(r2.get('peso_sismico') or 1, 1e-9) * 100, 1)} % de P)"],
                        ["Torsión accidental", (r2.get("torsion") or {}).get("texto", "—")],
                        ["Sismo vertical y combinación", "EZ = 2/3 del espectro horizontal; E = máx(EX + 0,3·EZ; 0,3·EX + EZ), ±"],
                    ], [32, 68])
        elif techos:
            r = techos[0]["resultado"]
            D.p("Cargas de las costaneras:")
            for t in r.get("resumen") or []:
                if opciones.get("final") and t.startswith("No se verific"):
                    continue
                D.vineta(t)

    # ---- combinaciones
    if inc("combinaciones"):
        D.h1("Combinaciones de carga", "combinaciones")
        D.p("Combinaciones de resistencia según NCh3171 §9.1.1:")
        for c in ("1,4D", "1,2D + 0,5Lr", "1,2D + 1,6Lr + 0,8W", "1,2D + 1,6W + 0,5Lr", "0,9D + 1,6W"):
            D.vineta(c)
        if ejes:
            D.vineta("1,2D + 1,4E")
            D.vineta("0,9D + 1,4E")
        D.p("Cada combinación con viento se aplica a todos sus casos" + (", y las con sismo en ambos sentidos" if ejes else "")
            + ". Para el suelo y las deformaciones se usan combinaciones de servicio.")

    # ---- modelo
    if inc("modelo") and ejes:
        D.h1("Modelo y análisis", "modelo")
        par0 = (ejes[0].get("ajustes") or {}).get("parametros") or {}
        D.vineta(f"Modelo plano de cada eje: barras de acero y muros de madera como elementos de lámina (malla de {f(par0.get('malla', 0.5), 1)} m), "
                 "con la rigidez al corte del Manual de madera (Ec. 2.22); muros unidos de forma solidaria a los pilares.")
        D.vineta("Análisis estático lineal (D, Lr, W) y modal espectral con masa concentrada y combinación CQC, con modos hasta "
                 "alcanzar el 90 % de la masa en cada dirección.")
        D.vineta("Pandeo de los elementos de acero: en el plano, largo entre arriostramientos; fuera del plano, el largo completo.")
        for e in ejes:
            r = e["resultado"]
            if inc("modelo", "modos"):
                if len(ejes) > 1:
                    D.h2(f"Modos de {eje_titulo(e)}")
                D.p(f"{eje_titulo(e)}: {r.get('n_modos_usados', '—')} modos; T1 = {f(r.get('T'), 3)} s; masa participante "
                    f"{f((r.get('masa_participante_x') or 0) * 100, 1)} % en X y {f((r.get('masa_participante_z') or 0) * 100, 1)} % en Z.")
            if inc("modelo", "reacciones"):
                D.h2(f"Reacciones basales por estado de carga — {eje_titulo(e)}")
                D.tabla(["Estado", "Σ Rx (tonf)", "Σ Ry (tonf)", "Desplaz. lateral máx. (mm)"],
                        [[c["caso"], f(c.get("reaccion_x"), 3), f(c.get("reaccion_y"), 3), f(c.get("dx_max_mm"), 2)]
                         for c in r.get("casos") or [] if c.get("caso") != "E"], [25, 25, 25, 25])

    # ---- acero
    if inc("acero") and ejes:
        D.h1("Diseño de elementos de acero (AISC 360-16)", "acero")
        for e in ejes:
            r = e["resultado"]
            if len(ejes) > 1:
                D.h2(eje_titulo(e))
            ac = r.get("acero") or []
            D.tabla(["Elemento", "Sección", "L (m)", "Pu (tonf)", "Mu (tonf·m)", "Vu (tonf)", "φPn", "φMn", "Combinación", "Razón"],
                    [[a.get("origen"), a.get("seccion"), f(a.get("L")), f(a.get("Pu")), f(a.get("Mu")), f(a.get("Vu")), f(a.get("phiPn"), 1),
                      f(a.get("phiMn")), _combo(a.get("combo")), _razon(a.get("razon"))] for a in ac],
                    [11, 10, 7, 8, 8, 8, 8, 8, 20, 8])
            if ac:
                D.nota(f"Razón máxima {f(max(a['razon'] for a in ac))}.")
            if inc("acero", "sismicos"):
                cf = r.get("columna_fuerte") or []
                om = r.get("omega0") or []
                if cf:
                    exc = all(c.get("excepcion") for c in cf if not c.get("cumple"))
                    D.vineta(f"Columna fuerte–viga débil (AISC 341 E3.4a): {sum(1 for c in cf if not c.get('cumple'))} de {len(cf)} nudos con "
                             f"ΣMpc/ΣMpb < 1" + ("; se aplica la excepción por carga axial baja" if exc else "") +
                             (". Informativo: el sistema no es SMF." if (r.get("sistema_acero") or "") != "SMF" else "."))
                if om:
                    D.vineta(f"Sobrerresistencia Ω0 (NCh433:2026 5.3.3): " + ("exigible en " + str(sum(1 for o in om if o.get("aplica"))) + " pilar(es)"
                             if any(o.get("aplica") for o in om) else "no exigible (carga sísmica axial baja)") +
                             f"; razón máxima con Ω0 {f(max(o.get('razon') or 0 for o in om))}.")

    # ---- madera
    if inc("madera") and ejes and any(e["resultado"].get("madera") for e in ejes):
        D.h1("Muros de madera de marco plataforma", "madera")
        for e in ejes:
            md = e["resultado"].get("madera") or []
            if not md:
                continue
            if len(ejes) > 1:
                D.h2(eje_titulo(e))
            cab = ["Muro", "L (m)", "H (m)", "v (tonf/m)", "vn (tonf/m)", "Combinación", "FU corte"]
            an = [12, 8, 8, 11, 11, 12, 10]
            if inc("madera", "anclajes"):
                cab += ["T anclaje (tonf)", "Combinación anclaje"]
                an += [12, 16]
            D.tabla(cab, [[m.get("muro"), f(m.get("L")), f(m.get("H")), f(m.get("v"), 3), f(m.get("vn")), m.get("combo"), _razon(m.get("FU"))]
                          + ([f(m.get("T_anclaje")), m.get("combo_anclaje")] if inc("madera", "anclajes") else []) for m in md], an)
            if inc("madera", "anclajes"):
                D.nota("T = V·H/L' − (gravedad)·L/(2L') (Manual, Ec. 3.29 a 3.31); anclaje de los bordes según la app (unión al pilar u hold-down).")

    # ---- deformaciones
    if inc("deformaciones") and ejes:
        D.h1("Deformaciones", "deformaciones")
        for e in ejes:
            dfs = e["resultado"].get("deformaciones") or []
            if len(ejes) > 1:
                D.h2(eje_titulo(e))
            if not inc("deformaciones", "todas"):
                tipos = {}
                for d in dfs:
                    k = "sísmica" if "sísmica" in d["tipo"] else "viento" if "viento" in d["tipo"] else "vertical"
                    if k not in tipos or d["razon"] > tipos[k]["razon"]:
                        tipos[k] = d
                dfs = list(tipos.values())
            D.tabla(["Verificación", "Elemento", "Combinación", "δ (mm)", "Límite (mm)", "Criterio", "Razón"],
                    [[d["tipo"], d["elemento"], d["combo"], f(d["delta_mm"]), f(d["limite_mm"], 1), d["criterio"], _razon(d["razon"])] for d in dfs],
                    [18, 12, 17, 9, 10, 24, 10])
            if e["resultado"].get("separacion_m") is not None:
                D.nota(f"Separación mínima a los deslindes y entre cuerpos (5.10.1): {f(e['resultado']['separacion_m'] * 100, 1)} cm.")

    # ---- fundaciones
    if inc("fundacion") and con_fun:
        D.h1("Fundaciones", "fundacion")
        for e in con_fun:
            fu = e["fundacion"]
            fp = ((e.get("ajustes") or {}).get("parametros") or {}).get("fundacion") or {}
            arm = fp.get("armaduras") or {}
            s = fu.get("resumen_suelo") or {}
            sec = fu.get("seccion") or {}
            D.h2(f"Fundación corrida — {eje_titulo(e)}")
            if inc("fundacion", "armaduras"):
                D.p(f"Viga continua de sección T invertida (pedestal {f(sec.get('b_pedestal', 0) * 100, 0)}×{f(sec.get('h_pedestal', 0) * 100, 0)} cm "
                    f"sobre zapata {f(sec.get('b_zapata', 0) * 100, 0)}×{f(sec.get('h_zapata', 0) * 100, 0)} cm"
                    + (f", sello a {f(fp['profundidad'])} m" if fp.get("profundidad") else "") +
                    f") sobre resortes de Winkler que trabajan solo en compresión; largo {f(fu.get('L'))} m; peso propio más suelo "
                    f"{f(fu.get('peso_propio_tonf_m'), 3)} tonf/m. Cargas: las reacciones de los apoyos del eje por estado de carga.")
                if arm:
                    D.p(f"Armaduras: pedestal {arm.get('ped_barras_por_cara')} φ{arm.get('ped_diametro_mm')} por cara con estribos "
                        f"φ{arm.get('ped_estribo_diametro_mm')}@{arm.get('ped_estribo_separacion_cm')} cm; zapata {arm.get('fun_barras_superiores')} "
                        f"φ{arm.get('fun_diametro_mm')} superiores y {arm.get('fun_barras_inferiores')} φ{arm.get('fun_diametro_mm')} inferiores, "
                        f"estribos φ{arm.get('fun_estribo_diametro_mm')}@{arm.get('fun_estribo_separacion_cm')} cm.")
            if inc("fundacion", "suelo"):
                ok = lambda c: {"t": "Sí" if c else "No", "bold": True, "color": VERDE if c else ROJO}
                srv = [c for c in fu.get("servicio") or [] if c.get("estable")]
                asent = max((c.get("asentamiento_max_mm") or 0 for c in srv), default=None)
                D.tabla(["Verificación (servicio)", "Resultado", "Combinación", "Exigencia", "Cumple"], [
                    ["Presión de contacto", f"FU {f(s.get('FU_presion_max'))}", s.get("combo_presion", "—"), f"≤ q adm = {f(s.get('q_adm'), 1)} tonf/m²", ok((s.get("FU_presion_max") or 0) <= 1)],
                    ["Área en compresión", f"{f((s.get('pct_compresion_min') or 0) * 100, 1)} %", s.get("combo_pct", "—"), "≥ 80 % (7.2.1)", ok((s.get("pct_compresion_min") or 0) >= 0.8)],
                    ["Deslizamiento", f"FS = {f(s.get('FS_desliz_min'))}", "—", "FS ≥ 1,5", ok((s.get("FS_desliz_min") or 99) >= 1.5)],
                    ["Asentamiento máximo", f"{f(asent)} mm", "—", "—", "—"],
                ], [22, 22, 18, 26, 12])
            if inc("fundacion", "hormigon"):
                filas = []
                for v in (fu.get("viga") or []) + (fu.get("zapata") or []) + (fu.get("pedestal") or []):
                    dem = (f"Mu {f(v['Mu_tonfm'])} tonf·m" if v.get("Mu_tonfm") is not None else f"Vu {f(v['Vu_tonf'])} tonf" if v.get("Vu_tonf") is not None
                           else f"Pu {f(v['Pu_tonf'])} tonf" if v.get("Pu_tonf") is not None else f"ρ = {f(v.get('valor'), 4)}")
                    cap = (f"φMn {f(v['phiMn_tonfm'])}" if v.get("phiMn_tonfm") is not None else f"φVn {f(v['phiVn_tonf'], 1)}" if v.get("phiVn_tonf") is not None
                           else f"φPn {f(v['phiPn_tonf'], 1)}" if v.get("phiPn_tonf") is not None else f"mín. {v.get('minimo')}")
                    res_ = ({"t": "cumple" if v["valor"] >= v["minimo"] else "no cumple", "bold": True, "color": VERDE if v["valor"] >= v["minimo"] else ROJO}
                            if v.get("valor") is not None and v.get("minimo") is not None else _razon(v.get("razon")))
                    filas.append([v["verificacion"], dem, cap, _combo(v.get("combo")), res_])
                D.tabla(["Verificación (ACI 318-19)", "Demanda", "Capacidad", "Combinación", "Razón"], filas, [42, 17, 14, 17, 10])

    # ---- costaneras
    if inc("costaneras") and techos:
        D.h1("Costaneras de techo (AISI S100-16)", "costaneras")
        for t in techos:
            r = t["resultado"]
            pc = r.get("parametros") or {}
            if len(techos) > 1:
                D.h2(t["nombre"])
            D.p(f"Acero conformado en frío, fy = {f(pc.get('fy', 270), 0)} MPa. Cargas: cubierta {f(pc.get('peso_cubierta', 15), 0)} kgf/m² más el peso "
                f"propio; Lr con Lo = {f(pc.get('lo_techo', 102), 0)} kgf/m²; viento de componentes y revestimientos (q_h = {f(r.get('q_h'), 1)} kgf/m²). "
                "Ancho colaborante: la mitad de la separación a cada lado. Tensores: "
                + ({"auto": "1 a L/2 hasta 6 m de luz y 2 a L/3 sobre 6 m", "L/2": "a L/2", "L/3": "a L/3", "ninguno": "sin tensores"}.get(pc.get("tensores", "auto"), pc.get("tensores")))
                + ". Vigas continuas sobre sus apoyos; se verifica el tramo de mayor luz.")
            if inc("costaneras", "propiedades"):
                D.tabla(["Tipo en Revit", "Perfil", "A (cm²)", "Ix (cm⁴)", "Se,x (cm³)", "Peso (kgf/m)", "φMnxo (kgf·m)", "φMny (kgf·m)", "φVn (kgf)"],
                        [[tp, p["perfil"], f(p["A_cm2"]), f(p["Ix_cm4"], 1), f(p["Sex_cm3"]), f(p["peso_kgf_m"]), f(p["phiMnxo_kgfm"], 0),
                          f(p["phiMny_kgfm"], 0), f(p["phiVn_kgf"], 0)] for tp, p in (r.get("propiedades") or {}).items()],
                        [18, 13, 8, 9, 9, 9, 11, 11, 12])
            cs = [c for c in r.get("costaneras") or []]
            if inc("costaneras", "tabla"):
                D.tabla(["Costanera", "Perfil", "Largo (m)", "Tramo verificado", "Zona", "Ancho (m)", "Manda", "Combinación", "FU"],
                        [[c["id"], c.get("perfil") or "—", f(c.get("L")),
                          f"{f(c['tramo_verificado']['luz'])} m · {c['tramo_verificado'].get('posicion', '')}" if c.get("tramo_verificado") else "—",
                          ", ".join(str(z) for z in (c.get("tramo_verificado") or {}).get("zonas") or []), f(c.get("s")),
                          ((c.get("manda") or {}).get("verificacion") or "").replace(": Mx/φMnx + My/φMny", ""), (c.get("manda") or {}).get("combo", ""),
                          _razon(c.get("FU"))] for c in cs],
                        [12, 13, 8, 14, 6, 8, 17, 13, 9])
            con = [c for c in cs if c.get("FU") is not None]
            if inc("costaneras", "detalle") and con:
                c0 = max(con, key=lambda c: c["FU"])
                D.h2(f"Costanera más solicitada ({c0['id']})")
                D.tabla(["Verificación", "Demanda", "Capacidad", "Combinación", "Razón"],
                        [[v["verificacion"], v["demanda"], v["capacidad"], v["combo"], _razon(v["razon"])] for v in c0["verificaciones"]],
                        [32, 20, 22, 16, 10])
                notas = [n for v in c0["verificaciones"] for n in v.get("notas") or []]
                if notas:
                    D.nota("; ".join(notas))

    # ---- conclusiones
    if inc("conclusiones"):
        D.h1("Conclusiones y observaciones", "conclusiones")
        resumen = []
        if ejes and inc("acero"):
            resumen.append(f"razón máxima de acero {f(max((a['razon'] for e in ejes for a in e['resultado'].get('acero') or []), default=None))}")
        if ejes and inc("madera"):
            resumen.append(f"de muros {f(max((m['FU'] for e in ejes for m in e['resultado'].get('madera') or []), default=None))}")
        if con_fun:
            resumen.append(f"presión de contacto {f(max(e['fundacion']['resumen_suelo'].get('FU_presion_max') or 0 for e in con_fun) * 100, 0)} % de la admisible")
        if techos:
            resumen.append(f"FU máximo de costaneras {f(max((c['FU'] for t in techos for c in t['resultado'].get('costaneras') or [] if c.get('FU') is not None), default=None))}")
        final = bool(opciones.get("final"))
        # versión final: sin las observaciones de temas pendientes o no cubiertos (solo el resumen y las notas propias)
        obs = observaciones(ejes, techos, caps) if inc("conclusiones", "automaticas") and not final else []
        extra = [x.strip() for x in (opciones.get("observaciones_extra") or "").splitlines() if x.strip()]
        if resumen:
            D.p("Con los datos ingresados: " + "; ".join(resumen) + "."
                + (" Antes de emitir la memoria definitiva deben resolverse las siguientes observaciones:" if (obs or extra) and not final
                   else (" Notas:" if extra else "")))
        for titulo, texto in obs:
            D.numerado((titulo + ". ",), texto)
        for linea in extra:
            D.numerado(linea)

    # figuras que aún no se pusieron (capítulo final o sin conclusiones)
    D.vaciar(getattr(D, "clave", None))
    resto = [f for v in D.pendientes.values() for f in v]
    if resto:
        D.h1("Figuras complementarias", None)
        for png, ley in resto:
            D.figura(png, ley)
        D.pendientes = {}

    if inc("firma"):
        D.p(f"{datos['ubicacion'].split(',')[0]}, {_fecha_texto(datos.get('fecha'))}" if datos.get("ubicacion") else _fecha_texto(datos.get("fecha")),
            antes=30, alinear=WD_ALIGN_PARAGRAPH.RIGHT)
        if empresa.get("firma"):
            par = D.d.add_paragraph()
            par.alignment = WD_ALIGN_PARAGRAPH.CENTER
            par.paragraph_format.space_before = Pt(30)
            par.paragraph_format.keep_together = True
            par.add_run().add_picture(io.BytesIO(empresa["firma"]), width=Cm(7.5))
            if datos.get("rut"):
                D.p(datos["rut"], alinear=WD_ALIGN_PARAGRAPH.CENTER, color=GRIS)
        else:
            D.p("______________________________", alinear=WD_ALIGN_PARAGRAPH.CENTER, antes=60, despues=0)
            D.p(datos.get("calculista") or "Ingeniero calculista", alinear=WD_ALIGN_PARAGRAPH.CENTER, despues=0)
            D.p(datos.get("rut") or "Nombre, RUT y firma", alinear=WD_ALIGN_PARAGRAPH.CENTER, color=GRIS)
    return D.bytes()
