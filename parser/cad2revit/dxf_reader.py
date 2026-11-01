"""Lectura de DXF con ezdxf. Todo se devuelve en METROS.

Para DWG: convertir antes a DXF con ODA File Converter (gratuito), ver README.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import ezdxf
from ezdxf import bbox

# $INSUNITS -> factor a metros
FACTOR_UNIDADES = {1: 0.0254, 2: 0.3048, 4: 0.001, 5: 0.01, 6: 1.0}
FACTOR_NOMBRE = {"mm": 0.001, "cm": 0.01, "m": 1.0}

Punto = tuple[float, float]


@dataclass
class Segmento:
    a: Punto
    b: Punto
    capa: str

    @property
    def largo(self) -> float:
        return math.dist(self.a, self.b)


@dataclass
class Insercion:
    nombre: str
    capa: str
    punto: Punto
    rotacion: float                 # grados
    extremos: tuple[Punto, Punto] | None   # bbox en planta (min, max)
    esquinas: list[Punto] = field(default_factory=list)  # geometría del bloque en mundo
    atributos: dict[str, str] = field(default_factory=dict)


@dataclass
class Texto:
    texto: str
    capa: str
    punto: Punto


@dataclass
class DibujoCAD:
    archivo: str
    factor: float
    segmentos: list[Segmento] = field(default_factory=list)
    polilineas_cerradas: list[tuple[list[Punto], str]] = field(default_factory=list)
    inserciones: list[Insercion] = field(default_factory=list)
    textos: list[Texto] = field(default_factory=list)

    def segmentos_en(self, patron: re.Pattern | None) -> list[Segmento]:
        return [s for s in self.segmentos if patron and patron.search(s.capa)]

    def inserciones_en(self, patron: re.Pattern | None) -> list[Insercion]:
        return [i for i in self.inserciones if patron and patron.search(i.capa)]

    def textos_en(self, patron: re.Pattern | None) -> list[Texto]:
        return [t for t in self.textos if patron and patron.search(t.capa)]

    def polilineas_en(self, patron: re.Pattern | None) -> list[list[Punto]]:
        return [p for p, capa in self.polilineas_cerradas if patron and patron.search(capa)]


def _factor(doc, unidades: str) -> float:
    if unidades and unidades != "auto":
        return FACTOR_NOMBRE[unidades]
    code = doc.header.get("$INSUNITS", 0)
    if code in FACTOR_UNIDADES:
        return FACTOR_UNIDADES[code]
    # Sin unidades declaradas: heurística por tamaño del dibujo
    ext = bbox.extents(doc.modelspace(), fast=True)
    if ext.has_data and max(ext.size.x, ext.size.y) > 500:
        return 0.001  # probablemente milímetros
    return 1.0


def leer(archivo: str, unidades: str = "auto") -> DibujoCAD:
    doc = ezdxf.readfile(archivo)
    msp = doc.modelspace()
    k = _factor(doc, unidades)
    dib = DibujoCAD(archivo=str(archivo), factor=k)

    def pt(v) -> Punto:
        return (round(v[0] * k, 6), round(v[1] * k, 6))

    def agregar_poli(puntos: list[Punto], cerrada: bool, capa: str):
        n = len(puntos)
        for i in range(n if cerrada else n - 1):
            a, b = puntos[i], puntos[(i + 1) % n]
            if a != b:
                dib.segmentos.append(Segmento(a, b, capa))
        if cerrada and n >= 3:
            dib.polilineas_cerradas.append((puntos, capa))

    for e in msp:
        t = e.dxftype()
        capa = e.dxf.get("layer", "0")
        if t == "LINE":
            dib.segmentos.append(Segmento(pt(e.dxf.start), pt(e.dxf.end), capa))
        elif t == "LWPOLYLINE":
            agregar_poli([pt(p) for p in e.get_points("xy")], e.closed, capa)
        elif t == "POLYLINE" and not e.is_3d_polyline:
            agregar_poli([pt(v.dxf.location) for v in e.vertices], e.is_closed, capa)
        elif t == "INSERT":
            esquinas: list[Punto] = []
            try:
                virt = list(e.virtual_entities())
                ext = bbox.extents(virt)
                extremos = (pt(ext.extmin), pt(ext.extmax)) if ext.has_data else None
                for v in virt:
                    if v.dxftype() == "LINE":
                        esquinas += [pt(v.dxf.start), pt(v.dxf.end)]
                    elif v.dxftype() == "LWPOLYLINE":
                        esquinas += [pt(p) for p in v.get_points("xy")]
                    elif v.dxftype() == "ARC":
                        esquinas += [pt(p) for p in v.flattening(0.01)]
            except Exception:  # bloque corrupto o vacío
                extremos = None
            atributos = {a.dxf.tag: a.dxf.text for a in e.attribs} if e.attribs else {}
            dib.inserciones.append(Insercion(
                nombre=e.dxf.name, capa=capa, punto=pt(e.dxf.insert),
                rotacion=float(e.dxf.get("rotation", 0.0)), extremos=extremos,
                esquinas=esquinas, atributos=atributos))
        elif t in ("TEXT", "MTEXT"):
            texto = e.plain_text() if t == "MTEXT" else e.dxf.text
            ins = e.dxf.insert
            dib.textos.append(Texto(texto.strip(), capa, pt(ins)))
    return dib
