"""Orquesta la lectura de planos y genera el modelo intermedio (JSON) que
consume el add-in de Revit. Toda la lógica de 'qué se modela' vive aquí; el
add-in solo construye lo que el JSON indica."""
from __future__ import annotations

import json
import string
from pathlib import Path

from shapely.geometry import Polygon

from . import __version__
from .aberturas import asignar_a_muros, clasificar_inserciones
from .config import Configuracion, ErrorConfiguracion
from .dxf_reader import DibujoCAD, leer
from . import entramado as entramado_mod
from .estructura import (asignar_tipo_muro, contorno_exterior, detectar_columnas, generar_fundaciones,
                         generar_vigas, marcar_estructurales)
from .muros import Muro, detectar_muros
from .niveles import ajustar_niveles, leer_cotas

ALTURA_POR_DEFECTO = 2.60


# ---------------------------------------------------------------- tipos según LOD
def _simplificar(tipo: dict, modo: str) -> dict:
    nucleo = next(c for c in tipo["capas"] if c["funcion"] == "estructura")
    if modo == "generico":
        return {"nombre": f"Genérico {round(tipo['espesor_total'] * 100)} cm", "estructural": tipo.get("estructural", False),
                "capas": [{"funcion": "estructura", "material": None, "espesor": tipo["espesor_total"]}]}
    return {"nombre": tipo["nombre"], "estructural": tipo.get("estructural", False),
            "capas": [{"funcion": "estructura", "material": nucleo["material"], "espesor": tipo["espesor_total"]}]}


def _tipos_salida(definidos: list[dict], usados: set[str], modo: str) -> tuple[list[dict], dict[str, str]]:
    """Devuelve los tipos a crear y un mapa nombre original -> nombre final."""
    out, mapa = [], {}
    for t in definidos:
        if t["nombre"] not in usados:
            continue
        t2 = dict(t) if modo == "compuesto" else _simplificar(t, modo)
        t2.pop("espesor_total", None)
        mapa[t["nombre"]] = t2["nombre"]
        if all(o["nombre"] != t2["nombre"] for o in out):
            out.append(t2)
    return out, mapa


def _partir_terminacion(tipo: dict) -> tuple[dict, dict | None]:
    """Separa las capas superiores no estructurales (terminación) de una losa."""
    capas = tipo["capas"]
    k = next(i for i, c in enumerate(capas) if c["funcion"] == "estructura")
    if k == 0:
        return tipo, None
    est = {"nombre": f"{tipo['nombre']} - estructura", "estructural": True, "capas": capas[k:]}
    ter = {"nombre": f"{tipo['nombre']} - terminación", "estructural": False, "capas": capas[:k]}
    return est, ter


# ---------------------------------------------------------------- contornos
def _contorno(dib: DibujoCAD, muros: list[Muro], cfg) -> list[tuple[float, float]] | None:
    losas = cfg.estructuracion.get("losas") or {}
    modo = losas.get("contorno", "auto")
    poly = None
    if modo in ("capa", "auto"):
        polis = [Polygon(p) for p in dib.polilineas_en(cfg.capa("losas")) if len(p) >= 3]
        if polis:
            poly = max(polis, key=lambda p: p.area)
    if poly is None and modo in ("muros", "auto"):
        poly = contorno_exterior(muros)
        if poly is not None:
            perim = [m.espesor for m in muros if m.perimetral] or [m.espesor for m in muros]
            e = sorted(perim)[len(perim) // 2]
            borde = losas.get("borde_en", "cara_exterior")
            if borde == "eje":
                poly = poly.buffer(-e / 2, join_style="mitre")
            elif borde == "cara_interior":
                poly = poly.buffer(-e, join_style="mitre")
    if poly is None or poly.is_empty:
        return None
    poly = poly.simplify(0.005)
    coords = [(round(x, 4), round(y, 4)) for x, y in list(poly.exterior.coords)[:-1]]
    return coords


def _nombres_ejes(n: int, estilo: str) -> list[str]:
    if estilo == "letras":
        letras = [c for c in string.ascii_uppercase if c not in "IO"]
        return [letras[i % len(letras)] * (i // len(letras) + 1) for i in range(n)]
    return [str(i + 1) for i in range(n)]


# ---------------------------------------------------------------- principal
def _verificar_archivos(cfg: Configuracion) -> None:
    faltan = [r for r in [p["archivo"] for p in cfg.proyecto.get("plantas") or []]
              + list(cfg.proyecto.get("elevaciones") or []) if not cfg.ruta(r).exists()]
    if faltan:
        raise ErrorConfiguracion("No se encuentran estos planos (revisa proyecto.yaml):\n  - " + "\n  - ".join(faltan))


def construir_modelo(cfg: Configuracion) -> dict:
    _verificar_archivos(cfg)
    lod = cfg.lod
    adv: list[str] = []
    unidades = cfg.capas_cad.get("unidades", "auto")
    det = cfg.deteccion

    # Niveles
    elevaciones = [leer(cfg.ruta(r), unidades) for r in cfg.proyecto.get("elevaciones") or []]
    cotas = leer_cotas(elevaciones, cfg.capas_cad.get("patron_nivel", r"([+-]?\d+[.,]\d+)"), cfg.capa("niveles"))
    niveles = ajustar_niveles(cfg.proyecto["niveles"], cotas, adv)
    orden = [n["nombre"] for n in niveles]

    def siguiente(nivel: str) -> str | None:
        i = orden.index(nivel)
        return orden[i + 1] if i + 1 < len(orden) else None

    modelo = {
        "version": __version__, "unidades": "m", "proyecto": cfg.proyecto.get("nombre", ""),
        "lod": lod, "sistema_estructural": cfg.estructuracion.get("sistema"),
        "materiales": cfg.materialidades.get("materiales", []),
        "niveles": niveles, "ejes": [], "tipos_muro": [], "tipos_losa": [],
        "muros": [], "aberturas": [], "losas": [], "cielos": [], "columnas": [], "vigas": [],
        "fundaciones": {"corridas": [], "aisladas": []}, "recintos": [], "entramados": [], "advertencias": adv,
    }
    cota = {n["nombre"]: n["cota"] for n in niveles}

    def espesor_tipo(nombre: str | None, grupos=("tipos_losa", "tipos_cubierta")) -> float:
        for g in grupos:
            for t in cfg.materialidades.get(g, []):
                if t["nombre"] == nombre:
                    return t.get("espesor_total", 0.0)
        return 0.0

    plantas = sorted(cfg.proyecto.get("plantas") or [], key=lambda p: orden.index(p["nivel"]))
    todos_muros: list[tuple[str, Muro]] = []
    tipos_losa_usados: set[str] = set()
    losa_def = cfg.materialidades.get("tipo_losa_por_defecto")
    cub_def = cfg.materialidades.get("tipo_cubierta_por_defecto")

    for idx, planta in enumerate(plantas):
        nivel = planta["nivel"]
        sup = siguiente(nivel)
        pref = nivel
        dib = leer(cfg.ruta(planta["archivo"]), unidades)

        # 1) Aberturas/columnas (se clasifican antes para ayudar a unir muros)
        abert_ins, col_ins = clasificar_inserciones(dib.inserciones, cfg)
        puntos_vano = [i.punto for i, _ in abert_ins]

        # 2) Muros
        muros = detectar_muros(dib.segmentos_en(cfg.capa("muros")), det, puntos_vano, pref)
        if not muros:
            adv.append(f"{planta['archivo']}: no se detectaron muros (¿capas correctas en capas_cad.yaml?)")
        for m in muros:
            asignar_tipo_muro(m, cfg, adv)
        marcar_estructurales(muros, cfg)
        todos_muros += [(nivel, m) for m in muros]

        # 3) Aberturas
        if lod["aberturas"] != "ninguna":
            for ab in asignar_a_muros(abert_ins, muros, cfg, pref, adv):
                modelo["aberturas"].append(dict(
                    id=ab.id, muro=ab.muro, categoria=ab.categoria, bloque_cad=ab.insercion.nombre,
                    modo=ab.modo or lod["aberturas"], familia=ab.familia, tipo=ab.tipo, centro=ab.centro,
                    ancho=ab.ancho, alto=ab.alto, antepecho=ab.antepecho))

        # 4) Columnas y vigas
        columnas = detectar_columnas(col_ins, dib.polilineas_en(cfg.capa("columnas")), cfg, pref)
        if lod["columnas"]:
            for c in columnas:
                c.update(nivel=nivel, nivel_superior=sup)
                if sup is None:
                    c["altura"] = ALTURA_POR_DEFECTO
                modelo["columnas"].append(c)
        if lod["vigas"]:
            if sup is None:
                if columnas:
                    adv.append(f"{nivel}: no hay nivel superior; no se generan vigas")
            else:
                for v in generar_vigas(columnas, muros, cfg, pref):
                    v["nivel"] = sup
                    modelo["vigas"].append(v)

        # 5) Fundaciones (sólo nivel más bajo)
        if lod["fundaciones"] and idx == 0:
            f = generar_fundaciones(muros, columnas if lod["columnas"] else [], cfg)
            modelo["fundaciones"]["corridas"] += f["corridas"]
            modelo["fundaciones"]["aisladas"] += f["aisladas"]

        # 6) Losas, cubierta y cielos
        contorno = _contorno(dib, muros, cfg)
        if contorno is None:
            adv.append(f"{nivel}: no se pudo determinar el contorno de losa")
        else:
            if lod["losas"] and losa_def:
                modelo["losas"].append(dict(id=f"{pref}-L001", nivel=nivel, tipo=losa_def,
                                            contorno=contorno, cubierta=False))
                tipos_losa_usados.add(losa_def)
            es_ultima = idx == len(plantas) - 1
            if lod["cubierta"] and es_ultima and (cfg.estructuracion.get("losas") or {}).get("generar_cubierta", True):
                if sup is None:
                    adv.append(f"{nivel}: no hay nivel superior para la cubierta; agrega un nivel 'Cubierta'")
                elif cub_def:
                    modelo["losas"].append(dict(id=f"{sup}-L001", nivel=sup, tipo=cub_def,
                                                contorno=contorno, cubierta=True))
                    tipos_losa_usados.add(cub_def)
            if lod["cielos"]:
                modelo["cielos"].append(dict(nivel=nivel, altura=lod["altura_cielo"], contorno=contorno))

        # 7) Recintos
        if lod["recintos"]:
            for t in dib.textos_en(cfg.capa("recintos")):
                modelo["recintos"].append(dict(nivel=nivel, nombre=t.texto, punto=t.punto))

        # 8) Ejes (desde la primera planta)
        if lod["ejes"] and idx == 0 and (cfg.estructuracion.get("ejes") or {}).get("leer_del_cad", True):
            ecfg = cfg.estructuracion.get("ejes") or {}
            lineas = dib.segmentos_en(cfg.capa("ejes"))
            vert = sorted([s for s in lineas if abs(s.b[0] - s.a[0]) < abs(s.b[1] - s.a[1])], key=lambda s: s.a[0])
            hori = sorted([s for s in lineas if abs(s.b[0] - s.a[0]) >= abs(s.b[1] - s.a[1])], key=lambda s: s.a[1])
            for grupo, estilo in ((vert, ecfg.get("nomenclatura_x", "numeros")),
                                  (hori, ecfg.get("nomenclatura_y", "letras"))):
                for s, nombre in zip(grupo, _nombres_ejes(len(grupo), estilo)):
                    modelo["ejes"].append(dict(nombre=nombre, inicio=s.a, fin=s.b))

        # 9) Entramado de madera (tipos de muro con sección "entramado")
        if lod.get("entramado"):
            es_ultima = idx == len(plantas) - 1
            losa_sup = (cub_def if es_ultima else losa_def) if lod["losas"] and sup else None
            for m in muros:
                conf = (cfg.tipo_muro(m.tipo) or {}).get("entramado")
                if not conf:
                    continue
                z0 = cota[nivel]
                z1 = (cota[sup] - espesor_tipo(losa_sup)) if sup else z0 + ALTURA_POR_DEFECTO
                abs_muro = [a for a in modelo["aberturas"] if a["muro"] == m.id]
                e = entramado_mod.generar(m, muros, conf, z0, z1, abs_muro)
                if e["piezas"]:
                    e["nivel"] = nivel
                    modelo["entramados"].append(e)

        for m in muros:
            d = dict(id=m.id, nivel=nivel, nivel_superior=sup, inicio=m.a, fin=m.b, espesor=m.espesor,
                     tipo=m.tipo, estructural=m.estructural, perimetral=m.perimetral, capa_cad=m.capa_cad)
            if sup is None:
                d["altura"] = ALTURA_POR_DEFECTO
            modelo["muros"].append(d)

    if lod["escaleras"]:
        adv.append("Escaleras: aún no se generan automáticamente (pendiente en hoja de ruta)")

    # Tipos finales según LOD
    usados = {m["tipo"] for m in modelo["muros"]}
    tipos_m, mapa_m = _tipos_salida(cfg.materialidades.get("tipos_muro", []), usados, lod["muros"])
    for m in modelo["muros"]:
        if m["tipo"] in mapa_m:
            m["tipo"] = mapa_m[m["tipo"]]
        elif all(t["nombre"] != m["tipo"] for t in tipos_m):   # genérico creado al vuelo
            tipos_m.append({"nombre": m["tipo"], "estructural": m["estructural"],
                            "capas": [{"funcion": "estructura", "material": None, "espesor": m["espesor"]}]})
    modelo["tipos_muro"] = tipos_m

    defs_losa = cfg.materialidades.get("tipos_losa", []) + cfg.materialidades.get("tipos_cubierta", [])
    tipos_l, mapa_l = _tipos_salida(defs_losa, tipos_losa_usados, lod["muros"])
    losas_final = []
    for l in modelo["losas"]:
        l["tipo"] = mapa_l.get(l["tipo"], l["tipo"])
        if lod["terminaciones"] and not l["cubierta"]:
            t = next(t for t in tipos_l if t["nombre"] == l["tipo"])
            est, ter = _partir_terminacion(t)
            if ter is not None:
                esp_ter = round(sum(c["espesor"] for c in ter["capas"]), 4)
                for nuevo in (est, ter):
                    if all(x["nombre"] != nuevo["nombre"] for x in tipos_l):
                        tipos_l.append(nuevo)
                losas_final.append(dict(l, tipo=est["nombre"], desfase=-esp_ter))
                losas_final.append(dict(l, id=l["id"].replace("-L", "-T"), tipo=ter["nombre"], desfase=0.0,
                                        terminacion=True))
                continue
        losas_final.append(dict(l, desfase=0.0))
    if lod["terminaciones"]:
        usados_l = {l["tipo"] for l in losas_final}
        tipos_l = [t for t in tipos_l if t["nombre"] in usados_l]
    modelo["losas"] = losas_final
    modelo["tipos_losa"] = tipos_l

    # Materiales: sólo los que se usan (más el estructural)
    usados_mat = {c["material"] for t in tipos_m + tipos_l for c in t["capas"] if c["material"]}
    if cfg.estructuracion.get("material_estructural"):
        usados_mat.add(cfg.estructuracion["material_estructural"])
    usados_mat |= {e["material"] for e in modelo["entramados"] if e.get("material")}
    modelo["materiales"] = [m for m in modelo["materiales"] if m["nombre"] in usados_mat]
    modelo["material_estructural"] = cfg.estructuracion.get("material_estructural")
    return json.loads(json.dumps(modelo))   # tuplas -> listas: el modelo es JSON puro


def resumen(modelo: dict) -> str:
    f = modelo["fundaciones"]
    filas = [
        ("Niveles", len(modelo["niveles"])), ("Ejes", len(modelo["ejes"])),
        ("Muros", len(modelo["muros"])),
        ("  estructurales", sum(m["estructural"] for m in modelo["muros"])),
        ("Aberturas", len(modelo["aberturas"])), ("Losas", len(modelo["losas"])),
        ("Cielos", len(modelo["cielos"])), ("Columnas", len(modelo["columnas"])),
        ("Vigas", len(modelo["vigas"])), ("Fundaciones corridas", len(f["corridas"])),
        ("Zapatas aisladas", len(f["aisladas"])), ("Recintos", len(modelo["recintos"])),
        ("Advertencias", len(modelo["advertencias"])),
    ]
    return "\n".join(f"  {k:<22}{v}" for k, v in filas)


def guardar(modelo: dict, salida: str | Path) -> None:
    Path(salida).write_text(json.dumps(modelo, ensure_ascii=False, indent=2), encoding="utf-8")
