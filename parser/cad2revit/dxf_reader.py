"""Lectura de DXF con ezdxf. Todo se devuelve en METROS.

Para DWG: convertir antes a DXF con ODA File Converter (gratuito), ver README.
"""
from __future__ import annotations

import math
import re
import threading
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


# ---------------------------------------------------------------- formato interno (caché)
# La primera lectura de un DXF extrae solo lo que el parser usa (segmentos, polilíneas
# cerradas, bloques y textos, ya en metros) a un JSON comprimido junto al plano:
#   <carpeta>/.cad2revit/<archivo>.<unidades>.geo.json.gz
# Las lecturas siguientes usan ese archivo (mucho más chico y rápido que el DXF) mientras
# el plano no cambie (mismo tamaño y fecha) y la versión del formato sea la misma.
FORMATO_INTERNO = 1


def _ruta_cache(archivo: str, unidades: str):
    from pathlib import Path
    a = Path(archivo)
    return a.parent / ".cad2revit" / f"{a.name}.{unidades or 'auto'}.geo.json.gz"


def _firma(archivo: str) -> dict:
    import os
    st = os.stat(archivo)
    return {"tam": st.st_size, "mtime": int(st.st_mtime_ns), "formato": FORMATO_INTERNO}


def _a_dict(d: "DibujoCAD") -> dict:
    return {
        "factor": d.factor,
        "segmentos": [[s.a, s.b, s.capa] for s in d.segmentos],
        "polilineas": [[p, c] for p, c in d.polilineas_cerradas],
        "inserciones": [[i.nombre, i.capa, i.punto, i.rotacion, i.extremos, i.esquinas, i.atributos] for i in d.inserciones],
        "textos": [[t.texto, t.capa, t.punto] for t in d.textos],
    }


def _de_dict(archivo: str, x: dict) -> "DibujoCAD":
    tp = lambda v: (v[0], v[1])   # noqa: E731
    d = DibujoCAD(archivo=str(archivo), factor=x["factor"])
    d.segmentos = [Segmento(tp(a), tp(b), c) for a, b, c in x["segmentos"]]
    d.polilineas_cerradas = [([tp(q) for q in pts], c) for pts, c in x["polilineas"]]
    d.inserciones = [Insercion(n, c, tp(p), r, (tp(e[0]), tp(e[1])) if e else None, [tp(q) for q in esq], at or {})
                     for n, c, p, r, e, esq, at in x["inserciones"]]
    d.textos = [Texto(t, c, tp(p)) for t, c, p in x["textos"]]
    return d


_CANDADOS: dict[str, threading.Lock] = {}
_CANDADO_GLOBAL = threading.Lock()


def leer(archivo: str, unidades: str = "auto", usar_cache: bool = True) -> DibujoCAD:
    """Lee un DXF usando el formato interno si está vigente; si no, lo crea.
    Un plano se lee una sola vez a la vez: si dos pedidos llegan juntos, el segundo espera la caché."""
    if not usar_cache:
        return _leer_dxf(archivo, unidades)
    with _CANDADO_GLOBAL:
        candado = _CANDADOS.setdefault(f"{archivo}|{unidades}", threading.Lock())
    with candado:
        return _leer_con_cache(archivo, unidades)


def _leer_con_cache(archivo: str, unidades: str) -> DibujoCAD:
    import gzip
    import json
    ruta = _ruta_cache(archivo, unidades)
    firma = _firma(archivo)
    try:
        with gzip.open(ruta, "rt", encoding="utf-8") as f:
            datos = json.load(f)
        if datos.get("firma") == firma:
            return _de_dict(archivo, datos)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    dib = _leer_dxf(archivo, unidades)
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        tmp = ruta.with_suffix(".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
            json.dump(dict(_a_dict(dib), firma=firma), f, separators=(",", ":"))
        tmp.replace(ruta)
    except OSError:
        pass                         # sin permiso de escritura: se sigue sin caché
    return dib


def _leer_dxf(archivo: str, unidades: str = "auto") -> DibujoCAD:
    try:
        doc = ezdxf.readfile(archivo)
    except ezdxf.DXFStructureError:
        from ezdxf import recover          # DXF con errores de estructura (p.ej. convertido desde DWG)
        doc, _ = recover.readfile(archivo)
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
