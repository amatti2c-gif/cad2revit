"""Vistas dentro de un mismo plano.

Es común que un DWG traiga la planta, las fachadas y los cortes dibujados lado a lado en el mismo
espacio modelo. Aquí se separan en "vistas" (grupos de dibujo separados por espacio vacío) y se
sugiere cuál es la planta y cuáles son elevaciones. El usuario confirma en la app.

Una vista se identifica por su caja [x0, y0, x1, y1] en metros, que se guarda en proyecto.yaml.
"""
from __future__ import annotations

import math
import re

from . import cortes as cortes_mod
from .dxf_reader import DibujoCAD
from .niveles import cota_en_texto
from .muros import largo_pareado_por_capa

SEPARACION = 3.0          # m de vacío que separan dos vistas
LARGO_IGNORADO = 80.0     # líneas más largas (marcos de lámina, calles) no unen vistas
MAX_VISTAS = 12
MARCA_NIVEL = re.compile(r"(?i)nivel|npt|level")   # bloques de cota de nivel (ArchiCAD: "Cota de Nivel_3")


def _celda(x: float, y: float, g: float) -> tuple[int, int]:
    return (math.floor(x / g), math.floor(y / g))


BANDA_MIN = 0.5          # m: ancho mínimo de una banda vacía entre dos vistas
CRUCES_MAX = 2           # líneas que pueden cruzar esa banda (línea de suelo, un eje largo)


def _cortes_xy(segs: list, minimo: int = 8) -> list[list]:
    """Corte recursivo en X/Y: separa el dibujo por bandas casi vacías (como se separan
    las vistas de una lámina). Banda: >= BANDA_MIN de ancho y <= CRUCES_MAX líneas cruzándola.
    Una banda vacía separa siempre; una cruzada por líneas (línea de suelo, un eje) solo si a ambos
    lados queda dibujo propio (>= `minimo` líneas que no la cruzan): así no se parte un rectángulo."""
    pendientes, hojas = [segs], []
    while pendientes:
        grupo = pendientes.pop()
        bandas = []
        for eje in (0, 1):
            lo = min(min(s.a[eje], s.b[eje]) for s in grupo)
            hi = max(max(s.a[eje], s.b[eje]) for s in grupo)
            if hi - lo < 2 * BANDA_MIN:
                continue
            paso = max(0.1, (hi - lo) / 4000)
            n = int((hi - lo) / paso) + 1
            delta = [0] * (n + 2)                      # cobertura por diferencias (rápido)
            for s in grupo:
                a, b = sorted((s.a[eje], s.b[eje]))
                delta[int((a - lo) / paso)] += 1
                delta[int((b - lo) / paso) + 1] -= 1
            cob, acc = [], 0
            for i in range(n):
                acc += delta[i]
                cob.append(acc)
            i = 0
            while i < n:
                if cob[i] <= CRUCES_MAX:
                    j = i
                    while j < n and cob[j] <= CRUCES_MAX:
                        j += 1
                    if i > 0 and j < n and (j - i) * paso >= BANDA_MIN:
                        bandas.append(((max(cob[i:j]) == 0, (j - i) * paso), eje,
                                       lo + i * paso, lo + j * paso))
                    i = j
                else:
                    i += 1
        partido = False
        for (vacia, _), eje, b0, b1 in sorted(bandas, reverse=True):
            corte = (b0 + b1) / 2
            lados = ([], [])
            for s in grupo:
                lados[int((s.a[eje] + s.b[eje]) / 2 > corte)].append(s)
            if not (lados[0] and lados[1]):
                continue
            if not vacia:
                propios = [sum(1 for s in l if max(s.a[eje], s.b[eje]) < b0 or min(s.a[eje], s.b[eje]) > b1)
                           for l in lados]
                if min(propios) < minimo:
                    continue
            pendientes.extend(lados)
            partido = True
            break
        if not partido:
            hojas.append(grupo)
    return hojas


def detectar(dib: DibujoCAD, es_abertura=None, patron_nivel: str | None = None,
             patron_muros: re.Pattern | None = None, det: dict | None = None) -> list[dict]:
    """Lista de vistas, de mayor a menor. Si el plano tiene una sola vista, devuelve una sola."""
    g = SEPARACION
    segs = [s for s in dib.segmentos if math.dist(s.a, s.b) <= LARGO_IGNORADO]
    if not segs:
        return []
    # 1) bandas vacías (vistas alineadas en filas/columnas, aunque estén a menos de 1 m)
    region_de = {}
    for r, hoja in enumerate(_cortes_xy(segs)):
        for s in hoja:
            region_de[id(s)] = r
    padre: dict[tuple[int, int], tuple[int, int]] = {}

    def raiz(c):
        while padre[c] != c:
            padre[c] = padre[padre[c]]
            c = padre[c]
        return c

    def unir(a, b):
        ra, rb = raiz(a), raiz(b)
        if ra != rb:
            padre[rb] = ra

    celda_seg = []
    for s in segs:
        r = region_de[id(s)]
        n = min(2000, max(1, int(math.dist(s.a, s.b) / (g / 2))))
        celdas = {(r,) + _celda(s.a[0] + (s.b[0] - s.a[0]) * k / n, s.a[1] + (s.b[1] - s.a[1]) * k / n, g)
                  for k in range(n + 1)}
        for c in celdas:
            padre.setdefault(c, c)
        primera = next(iter(celdas))
        for c in celdas:
            unir(primera, c)
        celda_seg.append(primera)
    for (r, cx, cy) in list(padre):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                v = (r, cx + dx, cy + dy)
                if v in padre:
                    unir((r, cx, cy), v)

    grupos: dict[tuple[int, int], dict] = {}
    for s, c in zip(segs, celda_seg):
        r = raiz(c)
        gr = grupos.setdefault(r, {"segs": [], "ins": [], "txt": []})
        gr["segs"].append(s)

    # textos y bloques: a la vista cuya caja los contiene (o la más cercana a menos de una celda)
    cajas_gr = {}
    for clave, gr in grupos.items():
        xs = [q[0] for s in gr["segs"] for q in (s.a, s.b)]
        ys = [q[1] for s in gr["segs"] for q in (s.a, s.b)]
        cajas_gr[clave] = (min(xs), min(ys), max(xs), max(ys))

    def grupo_de(p):
        mejor = None
        for clave, (x0, y0, x1, y1) in cajas_gr.items():
            d = math.hypot(max(x0 - p[0], 0, p[0] - x1), max(y0 - p[1], 0, p[1] - y1))
            if d <= g and (mejor is None or d < mejor[0]):
                mejor = (d, clave)
        return grupos[mejor[1]] if mejor else None

    for i in dib.inserciones:
        gr = grupo_de(i.punto)
        if gr is not None:
            gr["ins"].append(i)
    for t in dib.textos:
        gr = grupo_de(t.punto)
        if gr is not None:
            gr["txt"].append(t)

    det = det or {"espesor_min": 0.05, "espesor_max": 0.40, "largo_min": 0.20}
    rx_nivel = re.compile(patron_nivel) if patron_nivel else None
    vistas = []
    for gr in grupos.values():
        total = sum(math.dist(s.a, s.b) for s in gr["segs"])
        textos_nivel = sum(1 for t in gr["txt"] if rx_nivel and cota_en_texto(t.texto, rx_nivel) is not None)
        chico = (len(gr["segs"]) < 8 or total < 5) and not textos_nivel   # rótulo, norte… salvo que tenga título
        xs = [p[0] for s in gr["segs"] for p in (s.a, s.b)]
        ys = [p[1] for s in gr["segs"] for p in (s.a, s.b)]
        caja = [round(float(min(xs)), 2), round(float(min(ys)), 2), round(float(max(xs)), 2), round(float(max(ys)), 2)]
        ancho, alto = caja[2] - caja[0], caja[3] - caja[1]
        # fachada / corte: línea de suelo (horizontal casi de lado a lado, abajo) o marcas de nivel
        suelo = any(abs(s.b[1] - s.a[1]) <= 0.01 * max(abs(s.b[0] - s.a[0]), 1e-9)
                    and abs(s.b[0] - s.a[0]) >= 0.85 * ancho
                    and min(s.a[1], s.b[1]) <= caja[1] + 0.2 * alto for s in gr["segs"]) if ancho > 0 else False
        marcas = textos_nivel + sum(1 for i in gr["ins"] if MARCA_NIVEL.search(i.nombre))
        de_muros = [s for s in gr["segs"] if patron_muros and patron_muros.search(s.capa)] or gr["segs"]
        muros_m = sum(p for p, _ in largo_pareado_por_capa(de_muros, det).values()) / 2
        aberturas = sum(1 for i in gr["ins"] if es_abertura and es_abertura(i))
        vistas.append({"caja": caja, "ancho": round(caja[2] - caja[0], 1), "alto": round(caja[3] - caja[1], 1),
                       "lineas": len(gr["segs"]), "muros_m": round(float(muros_m), 1), "aberturas": aberturas,
                       "textos_nivel": textos_nivel, "marcas_nivel": marcas, "linea_suelo": suelo,
                       "bloques": sum(1 for i in gr["ins"] if not MARCA_NIVEL.search(i.nombre)), "chico": chico})
    for k, (tipo, letra, texto) in cortes_mod.titulos(dib, [v["caja"] for v in vistas]).items():
        vistas[k].update(titulo=texto, tipo_titulo=tipo, letra=letra)
    vistas = [v for v in vistas if not v.pop("chico") or v.get("titulo")]
    vistas.sort(key=lambda v: -(v["ancho"] * v["alto"] + v["lineas"]))
    vistas = vistas[:MAX_VISTAS]
    _sugerir(vistas)
    for k, v in enumerate(vistas):
        v["k"] = k
    return vistas


def _sugerir(vistas: list[dict]) -> None:
    """Un plano describe UN edificio: se sugiere UNA sola planta; las demás vistas son cortes, fachadas,
    la planta de techo o repeticiones de la planta (amoblada, con cotas…) y no generan estructura.

    - Fachada / corte: tiene título de corte o elevación, o una línea de suelo (horizontal de lado a lado, abajo).
      Las marcas de nivel no bastan: las plantas también las llevan.
    - Planta: la que no es fachada y tiene más puertas/ventanas, bloques (muebles, artefactos) y caras de muro.
    - Planta de techo: título de techo, o del mismo tamaño que la planta (± aleros) y casi sin bloques.
    - Planta repetida: mismo tamaño que la planta y con bloques (otra versión de la misma planta).
    """
    if not vistas:
        return

    def es_elevacion(v):
        t = v.get("tipo_titulo")
        return t in ("corte", "fachada") or (t is None and v["linea_suelo"])

    def puntaje(v):
        base = v["aberturas"] * 10 + v["bloques"] * 2 + v["muros_m"]
        t = v.get("tipo_titulo")
        if t in ("techo", "corte", "fachada") or es_elevacion(v):
            return base * 0.01
        return base * 3 + 1 if t == "planta" else base

    planta = max(vistas, key=puntaje)
    pw, ph = planta["ancho"], planta["alto"]
    mayor = max(pw, ph)

    def parecida(v):           # mismo tamaño que la planta, admitiendo aleros
        return abs(v["ancho"] - pw) <= 0.25 * pw + 2.5 and abs(v["alto"] - ph) <= 0.35 * ph + 2.5

    techo = next((v for v in vistas if v.get("tipo_titulo") == "techo"), None)
    if techo is None:
        candidatas = [v for v in vistas if v is not planta and not es_elevacion(v) and parecida(v)
                      and v["bloques"] <= 0.3 * max(planta["bloques"], 1) + 2 and v["aberturas"] == 0]
        techo = min(candidatas, key=lambda v: v["bloques"], default=None)
    for v in vistas:
        t = v.get("tipo_titulo")
        if v is planta:
            v["sugerido"], v["tipo"] = "planta", "planta"
        elif v is techo:
            v["sugerido"], v["tipo"] = "techo", "planta de techo"
        elif es_elevacion(v):
            v["tipo"] = {"corte": f"corte {v.get('letra') or ''}".strip(), "fachada": "fachada"}.get(t, "fachada o corte")
            # demasiado chica para ser una fachada o corte del edificio: detalle
            v["sugerido"] = "elevacion" if max(v["ancho"], v["alto"]) >= 0.2 * mayor else "no"
            if v["sugerido"] == "no":
                v["tipo"] = "detalle"
        else:
            v["sugerido"] = "no"
            v["tipo"] = "planta repetida" if parecida(v) else (
                {"planta": "otra planta"}.get(t) or "otro dibujo")


def recortar(dib: DibujoCAD, cajas: list[list[float]], margen: float = 0.5) -> DibujoCAD:
    """Solo lo que cae dentro de alguna de las cajas (vistas elegidas del plano)."""
    if not cajas:
        return dib

    def dentro(p):
        return any(c[0] - margen <= p[0] <= c[2] + margen and c[1] - margen <= p[1] <= c[3] + margen for c in cajas)

    return DibujoCAD(
        archivo=dib.archivo, factor=dib.factor,
        segmentos=[s for s in dib.segmentos if dentro(((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2))],
        polilineas_cerradas=[(p, capa) for p, capa in dib.polilineas_cerradas if p and dentro(p[0])],
        inserciones=[i for i in dib.inserciones if dentro(i.punto)],
        textos=[t for t in dib.textos if dentro(t.punto)],
    )


def archivo_de(entrada) -> str:
    """Las elevaciones pueden ser una ruta o {archivo, zona}."""
    return entrada["archivo"] if isinstance(entrada, dict) else entrada


def zonas_de(entrada) -> list[list[float]]:
    if not isinstance(entrada, dict):
        return []
    if entrada.get("zonas"):
        return entrada["zonas"]
    return [entrada["zona"]] if entrada.get("zona") else []
