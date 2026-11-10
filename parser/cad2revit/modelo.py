"""Orquesta la lectura de planos y genera el modelo intermedio (JSON) que
consume el add-in de Revit. Toda la lógica de 'qué se modela' vive aquí; el
add-in solo construye lo que el JSON indica."""
from __future__ import annotations

import math

import json
import string
from pathlib import Path

from shapely.geometry import Polygon

from . import __version__
from .aberturas import asignar_a_muros, clasificar_inserciones
from .config import Configuracion, ErrorConfiguracion
from .dxf_reader import Segmento, DibujoCAD, leer
from . import entramado as entramado_mod
from . import ejes as ejes_mod
from . import aberturas as aberturas_mod
from . import cortes as cortes_mod
from . import vistas as vistas_mod
from . import marcos as marcos_mod
from . import tramos as tramos_mod
from .estructura import (asignar_tipo_muro, contorno_exterior, detectar_columnas, generar_fundaciones,
                         generar_vigas, marcar_estructurales)
from .muros import quitar_encimados, sugerir_capas_muro, Muro, detectar_muros
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


def _caja(dib: DibujoCAD) -> list[float] | None:
    if not dib.segmentos:
        return None
    xs = [p[0] for s in dib.segmentos for p in (s.a, s.b)]
    ys = [p[1] for s in dib.segmentos for p in (s.a, s.b)]
    return [min(xs), min(ys), max(xs), max(ys)]


def _alero_de_cortes(vistas_altura: list[dict]) -> float | None:
    """Altura de alero: de las vistas que muestran la pendiente (cortes/fachadas transversales a la cumbrera);
    una fachada paralela a la cumbrera muestra la cumbrera arriba y no sirve para el alero."""
    inclinadas = [v for v in vistas_altura if v.get("pendiente", 0) >= 3]
    aleros = sorted(v["alero"] for v in (inclinadas or vistas_altura) if v.get("alero"))
    return round(aleros[len(aleros) // 2], 2) if aleros else None


def _limpiar_contorno(cont: list) -> list:
    """Contorno válido para un techo de Revit: sin puntos repetidos ni alineados, sin bordes de menos de
    5 cm y en sentido antihorario."""
    try:
        poli = Polygon(cont).buffer(0)
        if poli.geom_type != "Polygon":
            poli = max(poli.geoms, key=lambda g: g.area)
        poli = Polygon(poli.exterior).simplify(0.02)
        pts = [list(map(lambda v: round(v, 4), q)) for q in list(poli.exterior.coords)[:-1]]
        limpio = []
        for q in pts:
            if not limpio or math.dist(q, limpio[-1]) >= 0.05:
                limpio.append(q)
        if len(limpio) > 3 and math.dist(limpio[0], limpio[-1]) < 0.05:
            limpio.pop()
        if Polygon(limpio).exterior.is_ccw is False:
            limpio.reverse()
        return limpio if len(limpio) >= 3 else cont
    except Exception:
        return cont


def _ediciones_muros(modelo: dict, cfg, adv: list, hechos_q: set, hechos_e: set, avisar: bool) -> None:
    """Aplica muros_quitar y muros_editar del proyecto. hechos_* recuerdan qué ediciones ya calzaron con un muro;
    con avisar=True (última pasada) se advierte de las que no calzaron con ninguno."""
    def calza(d, q):
        qa, qb = tuple(q.get("inicio") or ()), tuple(q.get("fin") or ())
        return (d["nivel"] == q.get("nivel") and len(qa) == 2 and len(qb) == 2
                and ((math.dist(d["inicio"], qa) <= 0.2 and math.dist(d["fin"], qb) <= 0.2)
                     or (math.dist(d["inicio"], qb) <= 0.2 and math.dist(d["fin"], qa) <= 0.2)))

    fuera = set()
    for i, q in enumerate(cfg.proyecto.get("muros_quitar") or [], 1):
        hallados = {d["id"] for d in modelo["muros"] if calza(d, q)}
        if hallados:
            hechos_q.add(i)
        elif avisar and i not in hechos_q:
            adv.append(f"Muro quitado {i} ({q.get('nivel')}): ya no está en el modelo; no hay nada que quitar")
        fuera |= hallados
    if fuera:
        modelo["muros"] = [d for d in modelo["muros"] if d["id"] not in fuera]
        modelo["aberturas"] = [a for a in modelo["aberturas"] if a["muro"] not in fuera]
        adv.append(f"Muros quitados en la app: {', '.join(sorted(fuera))}")

    for i, q in enumerate(cfg.proyecto.get("muros_editar") or [], 1):
        hallados = [d for d in modelo["muros"] if calza(d, q)]
        if not hallados:
            if avisar and i not in hechos_e:
                adv.append(f"Muro editado {i} ({q.get('nivel')}): ya no está en el modelo; no se aplica")
            continue
        hechos_e.add(i)
        t = cfg.tipo_muro(q["tipo"]) if q.get("tipo") else None
        if q.get("tipo") and t is None:
            adv.append(f"Muro editado {i}: el lego '{q['tipo']}' no está en el catálogo; se mantiene el tipo")
        for d in hallados:
            if t:
                d["tipo"] = q["tipo"]
            if q.get("estructural") is not None:
                d["estructural"] = bool(q["estructural"])
            d["editado"] = True


def _planos_techo(modelo: dict, sup: str | None) -> list:
    """Planos del techo inclinado si el nivel superior es el de la cubierta (los marcos de ese piso lo siguen)."""
    cub_nivel = next((l["nivel"] for l in modelo["losas"] if l.get("cubierta")), None)
    return marcos_mod.planos_de_cubierta(modelo.get("cubierta")) if sup and sup == cub_nivel else []


def _calzar(techo: list, planta: list) -> tuple[float, float]:
    """Desplazamiento que lleva el contorno del techo sobre el de la planta con la mayor coincidencia
    (área común / área unida): búsqueda gruesa de ±4 m cada 10 cm alrededor de los centros y luego fina."""
    from shapely import affinity
    t0, p0 = Polygon(techo).buffer(0), Polygon(planta).buffer(0)
    if t0.is_empty or p0.is_empty:
        return 0.0, 0.0
    (ax0, ay0, ax1, ay1), (bx0, by0, bx1, by1) = t0.bounds, p0.bounds
    cx, cy = (bx0 + bx1 - ax0 - ax1) / 2, (by0 + by1 - ay0 - ay1) / 2

    def calce(dx, dy):
        t = affinity.translate(t0, dx, dy)
        u = t.union(p0).area
        return t.intersection(p0).area / u if u else 0.0

    mejor = (calce(cx, cy), cx, cy)
    for paso, radio in ((0.1, 4.0), (0.01, 0.1)):
        n = int(round(radio / paso))
        bx, by = mejor[1], mejor[2]
        for i in range(-n, n + 1):
            for j in range(-n, n + 1):
                c = calce(bx + i * paso, by + j * paso)
                if c > mejor[0] + 1e-9:
                    mejor = (c, bx + i * paso, by + j * paso)
    return round(mejor[1], 3), round(mejor[2], 3)


def _cubierta(contorno, techo_cad, vistas_altura, dib_planta, adv, modelo) -> dict:
    """Forma de la cubierta: contorno de la planta de techo (alineado con la planta) o de la planta,
    pendiente desde cortes/fachadas y cumbrera desde la planta de techo o desde el corte más inclinado."""
    pendiente = max((v["pendiente"] for v in vistas_altura), default=0.0)
    cont = [list(p) for p in contorno]
    cumbreras, limatones = [], 0
    if techo_cad:
        # la planta de techo está dibujada en otro lugar: se lleva sobre la planta buscando el desplazamiento
        # con el que más calza su forma (no basta centrar: alas y accesos sobresalen distinto en cada una)
        dx, dy = _calzar(techo_cad["contorno"], contorno)
        cont = [[round(x + dx, 3), round(y + dy, 3)] for x, y in techo_cad["contorno"]]
        cumbreras = [((a[0] + dx, a[1] + dy), (b[0] + dx, b[1] + dy)) for a, b in techo_cad["cumbreras"]]
        limatones = techo_cad["limatones"]
        lineas_techo = [((a[0] + dx, a[1] + dy), (b[0] + dx, b[1] + dy)) for a, b in techo_cad.get("lineas", [])]
    else:
        lineas_techo = []
    # cortes marcados en la planta: letra -> línea de corte
    letras = {v["letra"] for v in vistas_altura if v.get("letra")}
    lineas = cortes_mod.marcas_corte(dib_planta, letras) if letras else {}
    for v in vistas_altura:
        linea = lineas.get(v.get("letra"))
        modelo["cortes"].append(dict(titulo=v["titulo"], tipo=v["tipo"], letra=v.get("letra"),
                                     linea=[list(linea[0]), list(linea[1])] if linea else None,
                                     alero=v["alero"], cumbrera=v["cumbrera"], pendiente=v["pendiente"]))
        if v.get("letra") and not linea:
            adv.append(f"{v['titulo']}: no se encontró la marca de corte {v['letra']} en la planta")
    dir_cumbrera = None
    inclinados = [v for v in vistas_altura if v.get("letra") in lineas and v["pendiente"] >= 3]
    if inclinados and not cumbreras:
        a, b = lineas[max(inclinados, key=lambda v: v["pendiente"])["letra"]]
        dir_cumbrera = (-(b[1] - a[1]), b[0] - a[0])        # la cumbrera es perpendicular al corte
    cont = _limpiar_contorno(cont)
    forma = cortes_mod.cubierta(cont, cumbreras, limatones, pendiente, dir_cumbrera)
    # el achurado de la planta de techo dice, borde por borde, por dónde escurre el agua (pendiente) y dónde
    # hay frontón: así se leen techos con varias cumbreras (p. ej. un acceso a dos aguas perpendicular)
    if forma["forma"] != "plana" and lineas_techo:
        por_achurado = cortes_mod.bordes_por_achurado(cont, lineas_techo)
        conocidos = [i for i, v in enumerate(por_achurado) if v is not None]
        if len(conocidos) >= 0.6 * len(cont) and sum(1 for v in por_achurado if v) >= 2:
            bordes = [i for i, v in enumerate(por_achurado) if v or (v is None and i in forma["bordes_pendiente"])]
            if bordes != forma["bordes_pendiente"]:
                adv.append("Cubierta: pendientes de cada borde leídas del achurado de la planta de techo "
                           f"({len(bordes)} bordes con pendiente, {len(cont) - len(bordes)} frontones)")
            forma = {"forma": "varias aguas" if len(bordes) > 2 else forma["forma"], "bordes_pendiente": bordes}
    if forma["forma"] != "plana" and not forma["bordes_pendiente"]:
        adv.append("Cubierta: no se pudo saber qué bordes llevan pendiente; se deja plana")
        forma = {"forma": "plana", "bordes_pendiente": []}
    # cuerpos del techo: cumbrera, pendiente y altura de alero de cada agua (consistente con las elevaciones)
    extra = {}
    if forma["forma"] in ("dos aguas", "varias aguas"):
        frontones = [fa for v in vistas_altura for fa in v.get("aguas") or []]
        cota_cub = next((nv["cota"] for nv in modelo["niveles"][::-1]), None) if modelo.get("niveles") else None
        cu = cortes_mod.cuerpos(cont, forma["bordes_pendiente"], pendiente, cumbreras, frontones, cota_cub)
        if cu:
            extra = cu
            for k, c in enumerate(cu["cuerpos"]):
                bs = [b for b, ck in zip(forma["bordes_pendiente"], cu["cuerpos_bordes"]) if ck == k]
                bajo = min(z for z, ck in zip(cu["alturas_bordes"], cu["cuerpos_bordes"]) if ck == k)
                altos = [z - bajo for z, ck in zip(cu["alturas_bordes"], cu["cuerpos_bordes"]) if ck == k and z - bajo > 0.05]
                adv.append(f"Cubierta, cuerpo {k + 1} ({len(bs)} aguas): pendiente {c['pendiente']}°, cumbrera a "
                           f"{c['alto']:.2f} m sobre el nivel de cubierta" + (f"; {len(altos)} alero(s) más alto(s) para llegar a la "
                                                                  f"misma cumbrera (hasta {max(altos):.2f} m)" if altos else ""))
    if vistas_altura:
        adv.append(f"Cubierta {forma['forma']}" + (f", pendiente {pendiente}°" if forma["forma"] != "plana" else "")
                   + f" (de {len(vistas_altura)} cortes/fachadas" + (" y la planta de techo)" if techo_cad else ")"))
    return {"forma": forma["forma"], "contorno": cont, "pendiente": pendiente if forma["forma"] != "plana" else 0.0,
            "bordes_pendiente": forma["bordes_pendiente"],
            "cumbreras": [[list(a), list(b)] for a, b in cumbreras], "aleros_de_planta_de_techo": bool(techo_cad),
            **extra}


# ---------------------------------------------------------------- principal
def _verificar_archivos(cfg: Configuracion) -> None:
    faltan = [r for r in [p["archivo"] for p in cfg.proyecto.get("plantas") or []]
              + [vistas_mod.archivo_de(e) for e in cfg.proyecto.get("elevaciones") or []]
              + ([vistas_mod.archivo_de(cfg.proyecto["techo"])] if cfg.proyecto.get("techo") else [])
              if not cfg.ruta(r).exists()]
    if faltan:
        raise ErrorConfiguracion("No se encuentran estos planos (revisa proyecto.yaml):\n  - " + "\n  - ".join(faltan))


def construir_modelo(cfg: Configuracion) -> dict:
    _verificar_archivos(cfg)
    lod = cfg.lod
    adv: list[str] = []
    unidades = cfg.capas_cad.get("unidades", "auto")
    det = cfg.deteccion

    # Niveles
    elevaciones = [vistas_mod.recortar(leer(cfg.ruta(vistas_mod.archivo_de(e)), unidades), vistas_mod.zonas_de(e))
                   for e in cfg.proyecto.get("elevaciones") or []]
    patron_nivel = cfg.capas_cad.get("patron_nivel", r"([+-]?\d+[.,]\d+)")
    vistas_altura = []          # cortes y fachadas: alero, cumbrera, pendiente, y letra del corte
    for e, dib_e in zip(cfg.proyecto.get("elevaciones") or [], elevaciones):
        zonas = vistas_mod.zonas_de(e)
        caja = zonas[0] if zonas else _caja(dib_e)
        if caja is None:
            continue
        pf = cortes_mod.perfil(dib_e, caja, patron_nivel)
        tit = cortes_mod.titulos(leer(cfg.ruta(vistas_mod.archivo_de(e)), unidades), [caja]).get(0)
        if pf:
            vistas_altura.append(dict(pf, titulo=tit[2] if tit else Path(vistas_mod.archivo_de(e)).name,
                                      tipo=tit[0] if tit else "elevacion", letra=tit[1] if tit else None,
                                      aguas=[(w, g, round(y - pf["base"], 3)) for w, g, y in cortes_mod.aguas(dib_e, caja)]))
    techo_cad = None
    if cfg.proyecto.get("techo"):
        t = cfg.proyecto["techo"]
        techo_cad = cortes_mod.techo(vistas_mod.recortar(leer(cfg.ruta(vistas_mod.archivo_de(t)), unidades),
                                                         vistas_mod.zonas_de(t)))
        if techo_cad is None:
            adv.append("Planta de techo: no se pudo leer el contorno de la cubierta; se usa el de la planta")
    cotas = leer_cotas(elevaciones, cfg.capas_cad.get("patron_nivel", r"([+-]?\d+[.,]\d+)"), cfg.capa("niveles"))
    niveles = ajustar_niveles(cfg.proyecto["niveles"], cotas, adv)
    alero = _alero_de_cortes(vistas_altura)
    con_planta = {p["nivel"] for p in cfg.proyecto.get("plantas") or []}
    # niveles sin planta entre medio (p. ej. N2 en un edificio de un piso): se omiten; el último se conserva (techo)
    if con_planta and len(niveles) > 2:
        sobran = [n["nombre"] for n in niveles[:-1] if n["nombre"] not in con_planta]
        if sobran:
            niveles = [n for n in niveles if n["nombre"] not in sobran]
            adv.append(f"Niveles sin planta omitidos: {', '.join(sobran)} (el edificio tiene planta en "
                       f"{', '.join(n['nombre'] for n in niveles if n['nombre'] in con_planta)})")
    if alero and len(niveles) > 1 and niveles[-1]["nombre"] not in con_planta:
        sup_n, previo = niveles[-1], niveles[-2]["cota"]
        if alero > previo + 1.5 and abs(sup_n["cota"] - alero) > 0.05:
            adv.append(f"Nivel {sup_n['nombre']}: cota {sup_n['cota']} reemplazada por {alero} "
                       "(altura de alero leída de los cortes/fachadas)")
            sup_n["cota"] = alero
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
        "fundaciones": {"corridas": [], "aisladas": []}, "recintos": [], "entramados": [], "marcos": [], "tramos": [], "advertencias": adv,
        "cortes": [], "cubierta": None,
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
    legos = cfg.proyecto.get("legos") or {}
    losa_def = legos.get("losa") or cfg.materialidades.get("tipo_losa_por_defecto")
    cub_def = legos.get("cubierta") or cfg.materialidades.get("tipo_cubierta_por_defecto")

    pendiente_entramado: list[tuple[str, float, float]] = []
    for idx, planta in enumerate(plantas):
        nivel = planta["nivel"]
        sup = siguiente(nivel)
        pref = nivel
        dib = vistas_mod.recortar(leer(cfg.ruta(planta["archivo"]), unidades), vistas_mod.zonas_de(planta))

        # 1) Aberturas/columnas (se clasifican antes para ayudar a unir muros)
        abert_ins, col_ins = clasificar_inserciones(dib.inserciones, cfg)
        # puertas dibujadas como arco suelto (sin bloque): solo si el plano no trae bloques de puerta
        if not any(r.get("categoria") == "puerta" for _, r in abert_ins):
            arcos = aberturas_mod.puertas_de_arcos(dib.arcos, textos=dib.textos)
            if arcos:
                abert_ins += arcos
                adv.append(f"{nivel}: {len(arcos)} puertas leídas de los arcos de abatimiento (el plano no trae bloques de puerta)")
        puntos_vano = [i.punto for i, _ in abert_ins]

        # 2) Muros
        segs_muro = dib.segmentos_en(cfg.capa("muros"))
        if not segs_muro and not (cfg.proyecto.get("capas") or {}).get("muros"):
            sug = sugerir_capas_muro(dib.segmentos, det)
            if sug:
                usar = {c for c, _ in sug}
                segs_muro = [s for s in dib.segmentos if s.capa in usar]
                adv.append(f"{nivel}: ninguna capa se llama «muro»; se usaron las que por su dibujo son muros: "
                           + ", ".join(f"{c} ({m} m)" for c, m in sug)
                           + " (puedes cambiarlas en Planos → Capas de tu plano)")
        muros = detectar_muros(segs_muro, det, puntos_vano, pref)
        # ventanas sin bloque: vano en el muro + rótulo de medidas cerca ("Anc:120 H:150 Ant:100")
        if not any(r.get("categoria") == "ventana" for _, r in abert_ins):
            rotulos = aberturas_mod.rotulos_ventana(dib.textos)
            if rotulos:
                vent = aberturas_mod.ventanas_en_vanos(muros, rotulos, puntos_vano)
                if vent:
                    abert_ins += vent
                    puntos_vano += [i.punto for i, _ in vent]
                    muros = detectar_muros(segs_muro, det, puntos_vano, pref)     # el muro continúa sobre la ventana
                    adv.append(f"{nivel}: {len(vent)} ventanas leídas de los rótulos de medidas "
                               f"({len(rotulos)} rótulos en el plano)")
        muros, encimados = quitar_encimados(muros)
        if encimados:
            adv.append(f"{nivel}: {len(encimados)} 'muros' quedaban dentro de otros más gruesos (vidrios, marcos o muebles "
                       f"dibujados en el muro) y se descartaron: {', '.join(sorted({m.capa_cad for m in encimados}))}")
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
                    cub = _cubierta(contorno, techo_cad, vistas_altura, dib, adv, modelo)
                    modelo["losas"].append(dict(id=f"{sup}-L001", nivel=sup, tipo=cub_def,
                                                contorno=cub["contorno"], cubierta=True,
                                                pendiente=cub["pendiente"], bordes_pendiente=cub["bordes_pendiente"],
                                                forma=cub["forma"],
                                                **{k: cub[k] for k in ("pendientes_bordes", "alturas_bordes",
                                                                        "cuerpos_bordes", "cuerpos") if k in cub}))
                    modelo["cubierta"] = cub
                    tipos_losa_usados.add(cub_def)
            if lod["cielos"]:
                modelo["cielos"].append(dict(nivel=nivel, altura=lod["altura_cielo"], contorno=contorno))

        # 7) Recintos
        if lod["recintos"]:
            for t in dib.textos_en(cfg.capa("recintos")):
                modelo["recintos"].append(dict(nivel=nivel, nombre=t.texto, punto=t.punto))

        # 8) Ejes (desde la primera planta)
        if lod["ejes"] and idx == 0 and cfg.proyecto.get("ejes_base"):
            # ejes fijados en el proyecto (al editar la estructuración): no cambian aunque cambien los muros
            modelo["ejes"] += [dict(e, origen=e.get("origen") or "fijado") for e in cfg.proyecto["ejes_base"]]
        elif lod["ejes"] and idx == 0 and (cfg.estructuracion.get("ejes") or {}).get("leer_del_cad", True):
            ecfg = cfg.estructuracion.get("ejes") or {}
            lineas = ejes_mod.desde_cad(dib.segmentos_en(cfg.capa("ejes")))
            if lineas:
                modelo["ejes"] += ejes_mod.nombrar(lineas, ecfg, "plano CAD")
            elif muros and ecfg.get("generar_desde_muros", True):
                modelo["ejes"] += ejes_mod.nombrar(ejes_mod.desde_muros(muros), ecfg, "desde muros")
                adv.append(f"No se encontraron ejes en la capa de ejes del plano; se generaron {len(modelo['ejes'])} "
                           "ejes automáticos por las líneas de muros (elige tu capa de ejes en Planos → Capas de tu plano)")

        # 9) Entramado de madera: se genera al final, cuando ya se aplicaron las elecciones de los
        #    tramos (un tramo puede pasar a "tabique de madera")
        es_ultima = idx == len(plantas) - 1
        losa_sup = (cub_def if es_ultima else losa_def) if lod["losas"] and sup else None
        z0 = cota[nivel]
        pendiente_entramado.append((nivel, sup, z0, (cota[sup] - espesor_tipo(losa_sup)) if sup else z0 + ALTURA_POR_DEFECTO))

        # 10) Marcos de acero (líneas en capas de marcos)
        if lod.get("marcos"):
            segs_marco = dib.segmentos_en(cfg.capa("marcos"))
            modelo["marcos"] += marcos_mod.generar(segs_marco, cfg, nivel, sup, cota, pref, adv,
                                                   planos_techo=_planos_techo(modelo, sup))

        for m in muros:
            d = dict(id=m.id, nivel=nivel, nivel_superior=sup, inicio=m.a, fin=m.b, espesor=m.espesor,
                     tipo=m.tipo, estructural=m.estructural, perimetral=m.perimetral, capa_cad=m.capa_cad)
            if sup is None:
                d["altura"] = ALTURA_POR_DEFECTO
            modelo["muros"].append(d)

    # ejes antes de las ediciones de la app (para fijarlos en el proyecto)
    modelo["ejes_generados"] = [dict(e) for e in modelo["ejes"]]
    # Ediciones hechas en la app: ejes (mover, extender, agregar, borrar) y muros dibujados
    if cfg.proyecto.get("ejes_usuario"):
        modelo["ejes"] = ejes_mod.editar(modelo["ejes"], cfg.proyecto["ejes_usuario"], adv)
    for i, w in enumerate(cfg.proyecto.get("muros_usuario") or [], 1):
        if w.get("nivel") not in orden:
            adv.append(f"Muro dibujado {i}: el nivel '{w.get('nivel')}' no existe; se omite")
            continue
        t = cfg.tipo_muro(w.get("tipo")) if w.get("tipo") else None
        if w.get("tipo") and t is None:
            adv.append(f"Muro dibujado {i}: el lego '{w['tipo']}' no está en el catálogo; se usa espesor 15 cm")
        d = dict(id=f"{w['nivel']}-D{i:03d}", nivel=w["nivel"], nivel_superior=siguiente(w["nivel"]),
                 inicio=[float(v) for v in w["inicio"]], fin=[float(v) for v in w["fin"]],
                 espesor=(t or {}).get("espesor_total", 0.15), tipo=w.get("tipo") or "Genérico 15 cm",
                 estructural=bool(w.get("estructural", (t or {}).get("estructural", False))),
                 perimetral=False, capa_cad="dibujado en la app")
        if d["nivel_superior"] is None:
            d["altura"] = ALTURA_POR_DEFECTO
        modelo["muros"].append(d)

    # Muros quitados / editados en la app (se reconocen por su posición: nivel y extremos). Se aplican antes
    # de los tramos y otra vez después, para alcanzar también los muros que crea la elección de un tramo.
    hechos_q, hechos_e = set(), set()
    _ediciones_muros(modelo, cfg, adv, hechos_q, hechos_e, avisar=False)

    # Propuesta de estructuración por tramos de eje (y elecciones del proyecto)
    if lod.get("marcos") and cfg.estructuracion.get("propuesta_por_ejes", True):
        modelo["tramos"] = tramos_mod.proponer(modelo, cfg, [p["nivel"] for p in plantas], siguiente,
                                               cota, adv, marcos_mod)
    _ediciones_muros(modelo, cfg, adv, hechos_q, hechos_e, avisar=True)

    # Nivel buhardilla (opcional, apagado por defecto): sobre cada muro del último piso que queda bajo el techo
    # inclinado se agrega un muro desde el nivel de cubierta hasta el techo (frontones), unido al techo en Revit
    buhardilla = []
    cub_nivel = next((l["nivel"] for l in modelo["losas"] if l.get("cubierta")), None)
    planos_cub = _planos_techo(modelo, cub_nivel) if cub_nivel else []
    if cfg.proyecto.get("buhardilla") and planos_cub:
        # solo los muros perimetrales (frontones); los interiores siguen llegando horizontales a su nivel
        for d in [x for x in modelo["muros"] if x.get("nivel_superior") == cub_nivel and x.get("perimetral")]:
            ts, h = marcos_mod._perfil_techo(tuple(d["inicio"]), tuple(d["fin"]), planos_cub, 0.0, [])
            alto = max(h(t) for t in ts) if ts else 0.0
            if alto < 0.5:
                continue                       # muros de alero: el techo apenas sube sobre ellos (no es frontón)
            nuevo = dict(d, id=f"{d['id']}-B", nivel=cub_nivel, nivel_superior=None, altura=round(alto + 0.05, 3),
                         unir_techo=True, buhardilla=True)
            buhardilla.append(nuevo)
        modelo["muros"] += buhardilla
        if buhardilla:
            adv.append(f"Nivel buhardilla: {len(buhardilla)} muros desde {cub_nivel} hasta el techo (frontones)")
        else:
            adv.append("Nivel buhardilla activado, pero ningún muro queda bajo una zona inclinada del techo")
    elif cfg.proyecto.get("buhardilla"):
        adv.append("Nivel buhardilla activado, pero la cubierta no es inclinada: no hay muros que completar")

    # Entramado de madera (tipos de muro con sección "entramado"), con el tipo final de cada muro
    if lod.get("entramado"):
        for nivel, sup_e, z0, z1 in pendiente_entramado:
            planos = None                      # muros del piso: horizontales hasta el nivel superior
            muros = [Muro(id=d["id"], a=tuple(d["inicio"]), b=tuple(d["fin"]), espesor=d["espesor"],
                          capa_cad=d["capa_cad"], tipo=d["tipo"], estructural=d["estructural"])
                     for d in modelo["muros"] if d["nivel"] == nivel]
            for m in muros:
                conf = (cfg.tipo_muro(m.tipo) or {}).get("entramado")
                if not conf:
                    continue
                abs_muro = [a for a in modelo["aberturas"] if a["muro"] == m.id]
                e = entramado_mod.generar(m, muros, conf, z0, z1, abs_muro, planos_techo=planos)
                if e["piezas"]:
                    e["nivel"] = nivel
                    modelo["entramados"].append(e)

        # muros de buhardilla: entramado desde el nivel de cubierta hasta la línea del techo
        for d in buhardilla:
            conf = (cfg.tipo_muro(d["tipo"]) or {}).get("entramado")
            if not conf:
                continue
            m = Muro(id=d["id"], a=tuple(d["inicio"]), b=tuple(d["fin"]), espesor=d["espesor"], capa_cad=d["capa_cad"],
                     tipo=d["tipo"], estructural=d["estructural"])
            otros = [Muro(id=x["id"], a=tuple(x["inicio"]), b=tuple(x["fin"]), espesor=x["espesor"], capa_cad=x["capa_cad"])
                     for x in buhardilla]
            z0 = cota[cub_nivel]
            e = entramado_mod.generar(m, otros, conf, z0, z0, [], planos_techo=planos_cub)
            if e["piezas"]:
                e["nivel"] = cub_nivel
                modelo["entramados"].append(e)

    # Marcos dibujados en la app (momento o arriostrado, con su lego)
    dibujados = cfg.proyecto.get("marcos_usuario") or []
    if dibujados and lod.get("marcos"):
        nombres = {t["nombre"] for t in cfg.materialidades.get("tipos_marco", [])}
        for nivel in orden:
            items = []
            for i, f in enumerate(dibujados, 1):
                if f.get("nivel") != nivel:
                    continue
                if f.get("tipo") not in nombres:
                    adv.append(f"Marco dibujado {i}: el lego '{f.get('tipo')}' no está en el catálogo de marcos; se omite")
                    continue
                items.append((Segmento(tuple(f["inicio"]), tuple(f["fin"]), "dibujado en la app"), f["tipo"],
                              f"dibujado en la app #{i}"))
            if not items:
                continue
            vistas = {(round(c["punto"][0], 3), round(c["punto"][1], 3))
                      for f in modelo["marcos"] if f["nivel"] == nivel for c in f["columnas"]}
            n0 = 1 + sum(1 for f in modelo["marcos"] if f["nivel"] == nivel)
            sup = siguiente(nivel)
            modelo["marcos"] += marcos_mod.generar(items, cfg, nivel, sup, cota, nivel, adv, columnas_vistas=vistas,
                                                   numero_inicial=n0, planos_techo=_planos_techo(modelo, sup))
    for i, f in enumerate(dibujados, 1):
        if f.get("nivel") not in orden:
            adv.append(f"Marco dibujado {i}: el nivel '{f.get('nivel')}' no existe; se omite")

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
    usados_mat |= {x["material"] for x in modelo["columnas"] + modelo["vigas"] if x.get("material")}
    usados_mat |= {f["material"] for f in modelo["marcos"]}
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
