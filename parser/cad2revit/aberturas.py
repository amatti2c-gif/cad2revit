"""Puertas y ventanas: bloques del CAD -> aberturas alojadas en muros."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .dxf_reader import Insercion, Punto
from .muros import Muro, dist_punto_segmento, proyectar


@dataclass
class Abertura:
    id: str
    categoria: str          # puerta | ventana
    insercion: Insercion
    familia: str | None
    tipo: str | None
    ancho: float | None
    alto: float
    antepecho: float
    muro: str | None = None
    centro: Punto | None = None
    modo: str | None = None      # None = el del nivel de detalle; "hueco" = forzar vano


def regla_bloque(nombre: str, reglas: list[dict]) -> dict | None:
    for r in reglas:
        if re.search(r["bloque"], nombre):
            return r
    return None


def clasificar_inserciones(inserciones: list[Insercion], cfg) -> tuple[list[tuple[Insercion, dict]], list[Insercion]]:
    """Devuelve (aberturas con su regla, columnas)."""
    reglas = cfg.capas_cad.get("bloques") or []
    cp, cv, cc = cfg.capa("puertas"), cfg.capa("ventanas"), cfg.capa("columnas")
    aberturas, columnas = [], []
    for ins in inserciones:
        r = regla_bloque(ins.nombre, reglas)
        cat = r["categoria"] if r else None
        if cat is None:
            if cp and cp.search(ins.capa):
                cat = "puerta"
            elif cv and cv.search(ins.capa):
                cat = "ventana"
            elif cc and cc.search(ins.capa):
                cat = "columna"
        if cat in ("puerta", "ventana"):
            aberturas.append((ins, r or {"categoria": cat}))
        elif cat == "columna":
            columnas.append(ins)
    return aberturas, columnas


def asignar_a_muros(items: list[tuple[Insercion, dict]], muros: list[Muro], cfg, prefijo: str,
                    advertencias: list[str]) -> list[Abertura]:
    nd = cfg.nivel_detalle
    out = []
    for k, (ins, regla) in enumerate(items, 1):
        cat = regla["categoria"]
        gen = nd.get("familia_puerta_generica" if cat == "puerta" else "familia_ventana_generica") or {}
        ab = Abertura(
            id=f"{prefijo}-{'P' if cat == 'puerta' else 'V'}{k:03d}",
            categoria=cat, insercion=ins,
            familia=regla.get("familia") or gen.get("familia"),
            tipo=regla.get("tipo") or gen.get("tipo"),
            ancho=regla.get("ancho"),
            alto=float(regla.get("alto", 2.10 if cat == "puerta" else 1.20)),
            antepecho=float(regla.get("antepecho", 0.0 if cat == "puerta" else 0.90)),
        )
        # Muro más cercano al punto de inserción
        candidatos = sorted(muros, key=lambda m: dist_punto_segmento(ins.punto, m.a, m.b))
        muro = next((m for m in candidatos
                     if dist_punto_segmento(ins.punto, m.a, m.b) <= m.espesor / 2 + 0.15), None)
        if muro is None:
            advertencias.append(f"{ab.id} ({ins.nombre}) en {ins.punto}: no se encontró muro anfitrión; omitida")
            continue
        u = muro.direccion
        pts = ins.esquinas or ([ins.extremos[0], ins.extremos[1]] if ins.extremos else [ins.punto])
        ts = [proyectar(p, muro.a, u) for p in pts]
        t0, t1 = min(ts), max(ts)
        ancho_medido = t1 - t0
        if ab.ancho is None:
            ab.ancho = round(ancho_medido, 3) if ancho_medido > 0.3 else (0.9 if cat == "puerta" else 1.2)
        tc = (t0 + t1) / 2 if ancho_medido > 0.3 else proyectar(ins.punto, muro.a, u)
        tc = max(ab.ancho / 2, min(muro.largo - ab.ancho / 2, tc))
        ab.centro = (round(muro.a[0] + u[0] * tc, 4), round(muro.a[1] + u[1] * tc, 4))
        ab.muro = muro.id
        _regla_muro_delgado(ab, muro, cfg, advertencias)
        out.append(ab)
    return out


def _regla_muro_delgado(ab: Abertura, muro: Muro, cfg, advertencias: list[str]) -> None:
    """Las familias de puerta/ventana de Revit exigen un espesor mínimo de muro (las de la
    plantilla métrica de Revit 2027: 12,5 cm). En muros más delgados se usa la familia
    alternativa configurada o, si no hay, se deja solo el vano."""
    reglas = cfg.capas_cad.get("aberturas_en_muros_delgados") or {}
    r = reglas.get(ab.categoria) or {}
    minimo = r.get("espesor_minimo")
    if not minimo:
        return
    tipo = cfg.tipo_muro(muro.tipo) if muro.tipo else None
    espesor = tipo["espesor_total"] if tipo else muro.espesor
    if espesor >= minimo - 1e-6:
        return
    if r.get("familia"):
        ab.familia, ab.tipo = r["familia"], r.get("tipo")
        advertencias.append(f"{ab.id}: muro {muro.id} de {espesor * 100:.1f} cm (< {minimo * 100:.1f} cm); "
                            f"se usa la familia para muros delgados '{ab.familia}'")
    else:
        ab.modo = "hueco"
        advertencias.append(f"{ab.id}: la {ab.categoria} va en el muro {muro.id} de {espesor * 100:.1f} cm y las "
                            f"familias configuradas necesitan al menos {minimo * 100:.1f} cm; se deja solo el vano. "
                            "Define una familia para muros delgados en capas_cad.yaml (aberturas_en_muros_delgados).")


# ---------------------------------------------------------------- puertas y ventanas sin bloques
# Muchos planos (p. ej. exportados de ArchiCAD) no traen puertas/ventanas como bloques:
# la puerta es un ARCO suelto (abatimiento de la hoja) y la ventana es un vano en el muro con un
# rótulo de medidas ("Anc:120 H:150 Ant:100", en cm).

RX_ANCHO = re.compile(r"(?i)\banc(?:ho)?\.?\s*[:=]?\s*(\d+(?:[.,]\d+)?)")
RX_ALTO = re.compile(r"(?i)(?:^|[\s\n])(?:h|alt(?:o|ura)?)\.?\s*[:=]\s*(\d+(?:[.,]\d+)?)")
RX_ANTEPECHO = re.compile(r"(?i)\bant(?:epecho)?\.?\s*[:=]?\s*(\d+(?:[.,]\d+)?)")


def _medida(rx, texto: str) -> float | None:
    m = rx.search(texto)
    if not m:
        return None
    v = float(m.group(1).replace(",", "."))
    return v / 100 if v > 10 else v          # 120 -> 1,20 m ; 1.2 -> 1,2 m


def puertas_de_arcos(arcos, radio_min: float = 0.5, radio_max: float = 1.3) -> list[tuple[Insercion, dict]]:
    """Arcos de ~90° con radio de hoja de puerta: bisagra en el centro; el ancho es el radio."""
    out = []
    for a in arcos:
        if not (radio_min <= a.radio <= radio_max):
            continue
        v0 = (a.p0[0] - a.centro[0], a.p0[1] - a.centro[1])
        v1 = (a.p1[0] - a.centro[0], a.p1[1] - a.centro[1])
        ang = math.degrees(math.acos(max(-1.0, min(1.0, (v0[0] * v1[0] + v0[1] * v1[1]) / (a.radio ** 2)))))
        if not 75 <= ang <= 105:
            continue
        ins = Insercion(nombre="puerta (arco)", capa=a.capa, punto=a.centro, rotacion=0.0, extremos=None,
                        esquinas=[a.centro, a.p0, a.p1])
        out.append((ins, {"categoria": "puerta"}))
    return out


def rotulos_ventana(textos) -> list[dict]:
    """Rótulos con medidas de ventana: ancho obligatorio; alto y antepecho si vienen."""
    out = []
    for t in textos:
        ancho = _medida(RX_ANCHO, t.texto)
        if ancho is None or not 0.3 <= ancho <= 6:
            continue
        out.append({"punto": t.punto, "ancho": ancho, "alto": _medida(RX_ALTO, t.texto),
                    "antepecho": _medida(RX_ANTEPECHO, t.texto), "texto": t.texto})
    return out


def ventanas_en_vanos(muros: list[Muro], rotulos: list[dict], puntos_puerta: list[Punto],
                      distancia_max: float = 3.0) -> list[tuple[Insercion, dict]]:
    """Vanos (dos tramos colineales del mismo espesor separados 0,3–3,6 m, sin puerta) con un
    rótulo de ventana cerca: ventana con las medidas del rótulo, centrada en el vano."""
    vanos = []
    for i, m in enumerate(muros):
        u = m.direccion
        for n in muros[i + 1:]:
            if abs(m.espesor - n.espesor) > 0.011:
                continue
            v = n.direccion
            if abs(u[0] * v[1] - u[1] * v[0]) > 0.01:
                continue
            if dist_punto_recta(n.a, m.a, u) > 0.03 or dist_punto_recta(n.b, m.a, u) > 0.03:
                continue
            tm = (0.0, m.largo)
            tn = sorted((proyectar(n.a, m.a, u), proyectar(n.b, m.a, u)))
            if tn[0] >= tm[1]:
                g0, g1 = tm[1], tn[0]
            elif tm[0] >= tn[1]:
                g0, g1 = tn[1], tm[0]
            else:
                continue
            if not 0.3 <= g1 - g0 <= 3.6:
                continue
            pa = (m.a[0] + u[0] * g0, m.a[1] + u[1] * g0)
            pb = (m.a[0] + u[0] * g1, m.a[1] + u[1] * g1)
            if any(g0 - 0.1 <= proyectar(p, m.a, u) <= g1 + 0.1 and dist_punto_recta(p, m.a, u) <= m.espesor / 2 + 0.3
                   for p in puntos_puerta):
                continue                          # es el vano de una puerta
            vanos.append({"a": pa, "b": pb, "centro": ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2), "largo": g1 - g0})
    out, usados = [], set()
    pares = sorted(((math.dist(r["punto"], v["centro"]) + 2 * abs(r["ancho"] - v["largo"]), i, j)
                    for i, r in enumerate(rotulos) for j, v in enumerate(vanos)
                    if math.dist(r["punto"], v["centro"]) <= distancia_max))
    rot_usados = set()
    for _, i, j in pares:
        if i in rot_usados or j in usados:
            continue
        r, v = rotulos[i], vanos[j]
        rot_usados.add(i)
        usados.add(j)
        regla = {"categoria": "ventana", "ancho": round(min(r["ancho"], v["largo"]), 3)}
        if r["alto"]:
            regla["alto"] = r["alto"]
        if r["antepecho"] is not None:
            regla["antepecho"] = r["antepecho"]
        ins = Insercion(nombre="ventana (rótulo)", capa="rótulo", punto=v["centro"], rotacion=0.0, extremos=None,
                        esquinas=[v["a"], v["b"]])
        out.append((ins, regla))
    # sin vano (la ventana está dibujada dentro del muro): sobre el muro más cercano frente al rótulo
    ocupados = [(i.punto, r.get("ancho") or 0.9) for i, r in out] + [(p, 0.9) for p in puntos_puerta]
    for i, r in enumerate(rotulos):
        if i in rot_usados:
            continue
        mejor = None
        for m in muros:
            u = m.direccion
            t = proyectar(r["punto"], m.a, u)
            d = dist_punto_recta(r["punto"], m.a, u)
            if d > distancia_max * 0.7 or not (-0.2 <= t <= m.largo + 0.2) or m.largo < r["ancho"]:
                continue
            if mejor is None or d < mejor[0]:
                mejor = (d, m, t)
        if mejor is None:
            continue
        _, m, t = mejor
        u = m.direccion
        t = max(r["ancho"] / 2, min(m.largo - r["ancho"] / 2, t))
        c = (m.a[0] + u[0] * t, m.a[1] + u[1] * t)
        if any(math.dist(c, q) < (r["ancho"] + w) / 2 for q, w in ocupados):
            continue                              # se tocaría con otra ventana o con una puerta
        regla = {"categoria": "ventana", "ancho": r["ancho"]}
        if r["alto"]:
            regla["alto"] = r["alto"]
        if r["antepecho"] is not None:
            regla["antepecho"] = r["antepecho"]
        a_, b_ = (c[0] - u[0] * r["ancho"] / 2, c[1] - u[1] * r["ancho"] / 2), (c[0] + u[0] * r["ancho"] / 2, c[1] + u[1] * r["ancho"] / 2)
        out.append((Insercion(nombre="ventana (rótulo)", capa="rótulo", punto=c, rotacion=0.0, extremos=None,
                              esquinas=[a_, b_]), regla))
        ocupados.append((c, r["ancho"]))
    return out


def dist_punto_recta(p: Punto, origen: Punto, u: Punto) -> float:
    return abs((p[0] - origen[0]) * u[1] - (p[1] - origen[1]) * u[0])
