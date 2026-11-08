"""Propuesta de estructuración por tramos de eje.

Un "tramo" es el trozo de un eje entre dos ejes consecutivos que lo cruzan (p. ej.
"A:1-2" = eje A entre los ejes 1 y 2). Para cada tramo y cada piso se analiza lo que
hay en el plano (muros, su carácter estructural y sus vanos) y se propone una
tipología: muro, marco de momento, marco arriostrado o nada. El proyecto puede
cambiar la propuesta por tramo (todos los pisos) o por tramo y piso:

    tramos:
      "A:1-2": arriostrado          # todos los pisos
      "2:A-B@N2": momento           # solo el piso N2
"""
from __future__ import annotations

import math

from .dxf_reader import Segmento

OPCIONES = {"auto", "muro", "tabique", "momento", "arriostrado", "ninguno"}
SISTEMA = {"momento": "momento", "arriostrado": "arriostrado_x"}


def separar(eleccion: str) -> tuple[str, str | None]:
    """'tabique:TM-11 Tabique madera' -> ('tabique', 'TM-11 Tabique madera'); 'momento' -> ('momento', None)."""
    base, _, tipo = (eleccion or "").partition(":")
    return base, (tipo.strip() or None)


def valida(eleccion: str, tipos_muro: set[str] | None = None) -> bool:
    base, tipo = separar(eleccion)
    if base not in OPCIONES:
        return False
    if tipo is None:
        return True
    return base in ("muro", "tabique") and (tipos_muro is None or tipo in tipos_muro)


def _interseccion(a1, b1, a2, b2):
    d1 = (b1[0] - a1[0], b1[1] - a1[1])
    d2 = (b2[0] - a2[0], b2[1] - a2[1])
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < 1e-9:
        return None
    t = ((a2[0] - a1[0]) * d2[1] - (a2[1] - a1[1]) * d2[0]) / den
    u = ((a2[0] - a1[0]) * d1[1] - (a2[1] - a1[1]) * d1[0]) / den
    return t, u


def tramos_de_ejes(ejes: list[dict]) -> list[dict]:
    out = []
    for g in ejes:
        a, b = g["inicio"], g["fin"]
        L = math.dist(a, b)
        if L < 1e-6:
            continue
        cortes = []
        for o in ejes:
            if o is g:
                continue
            r = _interseccion(a, b, o["inicio"], o["fin"])
            if r is None:
                continue
            t, u = r
            tol_g, tol_o = 0.05 / L, 0.05 / max(math.dist(o["inicio"], o["fin"]), 1e-6)
            if -tol_g <= t <= 1 + tol_g and -tol_o <= u <= 1 + tol_o:
                cortes.append((t, o["nombre"]))
        cortes.sort()
        dx, dy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        for (t1, n1), (t2, n2) in zip(cortes, cortes[1:]):
            p1 = [round(a[0] + (b[0] - a[0]) * t1, 4), round(a[1] + (b[1] - a[1]) * t1, 4)]
            p2 = [round(a[0] + (b[0] - a[0]) * t2, 4), round(a[1] + (b[1] - a[1]) * t2, 4)]
            largo = math.dist(p1, p2)
            if largo < 0.05:
                continue
            out.append({"id": f"{g['nombre']}:{n1}-{n2}", "eje": g["nombre"], "entre": [n1, n2],
                        "inicio": p1, "fin": p2, "largo": round(largo, 3),
                        "direccion": "X" if abs(dx) >= abs(dy) else "Y", "u": [dx, dy]})
    return out


def _analizar(tr: dict, muros: list[dict], aberturas: list[dict]) -> dict:
    a, u, L = tr["inicio"], tr["u"], tr["largo"]

    def proy(p):
        return (p[0] - a[0]) * u[0] + (p[1] - a[1]) * u[1]

    def dist(p):
        return abs((p[0] - a[0]) * u[1] - (p[1] - a[1]) * u[0])

    cub_est = cub_todo = 0.0
    tipo_est = None
    tipos_tab: dict[str, float] = {}
    muros_tramo = set()
    for m in muros:
        mx, my = m["fin"][0] - m["inicio"][0], m["fin"][1] - m["inicio"][1]
        lm = math.hypot(mx, my)
        if lm < 1e-6 or abs(mx / lm * u[1] - my / lm * u[0]) > 0.02:
            continue                                   # no es paralelo
        tol = m["espesor"] / 2 + 0.05
        if dist(m["inicio"]) > tol or dist(m["fin"]) > tol:
            continue                                   # no está sobre el eje
        t0, t1 = sorted((proy(m["inicio"]), proy(m["fin"])))
        solape = max(0.0, min(t1, L) - max(t0, 0.0))
        if solape <= 0.01:
            continue
        muros_tramo.add(m["id"])
        cub_todo += solape
        if m["estructural"]:
            cub_est += solape
            tipo_est = tipo_est or m["tipo"]
        else:
            tipos_tab[m["tipo"]] = tipos_tab.get(m["tipo"], 0) + solape
    vanos = sum(ab["ancho"] for ab in aberturas
                if ab["muro"] in muros_tramo and -0.01 <= proy(ab["centro"]) <= L + 0.01)
    cob_e, cob_t, rv = min(1, cub_est / L), min(1, cub_todo / L), min(1, vanos / L)

    if L < 1.0:
        prop, motivo = "ninguno", f"Tramo muy corto ({L:.2f} m)"
    elif cob_e >= 0.8 and rv < 0.4:
        prop, motivo = "muro", f"Muro estructural {tipo_est} cubre el {cob_e:.0%} del tramo"
    elif cob_e >= 0.8:
        prop, motivo = "momento", (f"Muro estructural con vanos que ocupan el {rv:.0%}: "
                                   "el marco de momento deja libres los vanos")
    elif cob_t >= 0.5:
        tipo_tab = max(tipos_tab, key=tipos_tab.get) if tipos_tab else "tabique"
        prop, motivo = "tabique", (f"Tabiquería no estructural ({tipo_tab}) cubre el {cob_t:.0%} del tramo: "
                                   "no se pone marco (elige un marco si este eje debe resistir)")
    elif rv > 0:
        prop, motivo = "momento", (f"Tramo libre con vanos ({rv:.0%} del largo): "
                                   "el marco de momento deja libres los vanos")
    else:
        prop, motivo = "arriostrado", ("Sin muro estructural ni vanos: el marco arriostrado en X "
                                       "es más rígido y liviano")
    return {"propuesta": prop, "motivo": motivo, "cobertura_muro_estructural": round(cob_e, 2),
            "cobertura_muros": round(cob_t, 2), "vanos": round(rv, 2), "muros": sorted(muros_tramo)}


def _aplicar_muro(modelo, cfg, tr, an, nivel, sup, final, tipo, advertencias) -> str | None:
    """Muro estructural o tabique en el tramo: con lego elegido, los muros del plano en el tramo pasan a
    ese tipo (si no hay muro en el plano, se crea a lo largo del tramo); sin lego, se conserva el tipo y
    solo cambia su carácter (estructural o no)."""
    estructural = final == "muro"
    ids = set(an["muros"])
    del_tramo = [m for m in modelo["muros"] if m["id"] in ids]
    if not tipo and an["cobertura_muros"] < 0.3:
        tipo = _lego_muro_por_defecto(cfg, estructural)       # tramo sin muro en el plano: se agrega uno
        if tipo:
            advertencias.append(f"Tramo {tr['id']} ({nivel}): se agrega un {'muro' if estructural else 'tabique'} "
                                f"'{tipo}' (no había muro en el plano)")
    if tipo:
        if an["cobertura_muros"] < 0.3:
            t = cfg.tipo_muro(tipo) or {}
            n = 1 + sum(1 for m in modelo["muros"] if m["nivel"] == nivel and m["id"].startswith(f"{nivel}-T"))
            nuevo = dict(id=f"{nivel}-T{n:03d}", nivel=nivel, nivel_superior=sup, inicio=list(tr["inicio"]),
                         fin=list(tr["fin"]), espesor=t.get("espesor_total", 0.1), tipo=tipo, estructural=estructural,
                         perimetral=False, capa_cad=f"tramo {tr['id']}")
            if sup is None:
                nuevo["altura"] = 2.6
            modelo["muros"].append(nuevo)
            an["muros"] = [nuevo["id"]]
            return tipo
        for m in del_tramo:
            m["tipo"], m["estructural"] = tipo, estructural
        return tipo
    if not del_tramo:
        advertencias.append(f"Tramo {tr['id']} ({nivel}): se eligió '{final}' pero no hay muro en el plano; "
                            "elige un lego de muro para crearlo")
        return None
    for m in del_tramo:
        m["estructural"] = estructural
    return del_tramo[0]["tipo"]


def _lego_muro_por_defecto(cfg, estructural: bool) -> str | None:
    """Lego para un muro agregado en un tramo vacío: el muro por defecto del catálogo (o el primer estructural);
    para tabique, el que las reglas usan para tabiques (o el primer no estructural)."""
    mat = cfg.materialidades
    tipos = mat.get("tipos_muro", [])
    if estructural:
        d = mat.get("tipo_muro_por_defecto")
        t = cfg.tipo_muro(d) if d else None
        if t and t.get("estructural"):
            return d
        return next((x["nombre"] for x in tipos if x.get("estructural")), d)
    for r in mat.get("reglas_muro", []):
        if "tabiq" in str(r.get("capa_cad", "")).lower():
            return r["tipo"]
    return next((x["nombre"] for x in tipos if not x.get("estructural")), None)


def _lego_por_sistema(cfg, sistema: str) -> str | None:
    legos = cfg.proyecto.get("legos") or {}
    clave = {"momento": "marco_momento", "arriostrado_x": "marco_arriostrado"}[sistema]
    if legos.get(clave):
        return legos[clave]
    return next((t["nombre"] for t in cfg.materialidades.get("tipos_marco", []) if t.get("sistema") == sistema), None)


def _cubierto(tr: dict, lineas: list) -> bool:
    """El tramo está cubierto (>= 50 % de su largo) por alguna de las líneas (marcos dibujados en la app)."""
    a, L = tr["inicio"], tr["largo"]
    u = ((tr["fin"][0] - a[0]) / L, (tr["fin"][1] - a[1]) / L)
    for p, q in lineas:
        d = lambda r: abs((r[0] - a[0]) * u[1] - (r[1] - a[1]) * u[0])     # noqa: E731
        if d(p) > 0.3 or d(q) > 0.3:
            continue
        t0, t1 = sorted(((p[0] - a[0]) * u[0] + (p[1] - a[1]) * u[1], (q[0] - a[0]) * u[0] + (q[1] - a[1]) * u[1]))
        if min(t1, L) - max(t0, 0.0) >= 0.5 * L:
            return True
    return False


def proponer(modelo: dict, cfg, niveles_con_planta: list[str], siguiente, cota, advertencias: list[str],
             marcos_mod) -> list[dict]:
    """Analiza los tramos, aplica las elecciones del proyecto y agrega los marcos resultantes
    a modelo["marcos"]. Devuelve la lista de tramos con propuesta y elección por piso."""
    tramos = tramos_de_ejes(modelo["ejes"])
    if not tramos:
        if modelo["ejes"]:
            advertencias.append("Los ejes del plano no se cruzan: no hay tramos para proponer estructuración")
        return []
    elecciones = cfg.proyecto.get("tramos") or {}
    tipos = {t["nombre"] for t in cfg.materialidades.get("tipos_muro", [])}
    for clave, v in list(elecciones.items()):
        if not valida(v, tipos):
            advertencias.append(f"tramos: opción '{v}' de '{clave}' no válida (usa {sorted(OPCIONES)} o "
                                "'muro:<lego>' / 'tabique:<lego>' con un lego de muro del catálogo); se ignora")
            elecciones = {k: x for k, x in elecciones.items() if k != clave}
    ids = {t["id"] for t in tramos}
    for clave in elecciones:
        if clave.split("@")[0] not in ids:
            advertencias.append(f"tramos: '{clave}' no corresponde a ningún tramo de los ejes actuales")

    for nivel in niveles_con_planta:
        sup = siguiente(nivel)
        muros = [m for m in modelo["muros"] if m["nivel"] == nivel]
        abs_n = [a for a in modelo["aberturas"] if a["muro"] in {m["id"] for m in muros}]
        nuevos = []
        resistentes = {"X": 0, "Y": 0}
        for tr in tramos:
            an = _analizar(tr, muros, abs_n)
            eleccion = elecciones.get(f"{tr['id']}@{nivel}") or elecciones.get(tr["id"]) or "auto"
            final, tipo = separar(an["propuesta"] if eleccion == "auto" else eleccion)
            if final in ("muro", "tabique"):
                tipo = _aplicar_muro(modelo, cfg, tr, an, nivel, sup, final, tipo, advertencias)
            if final in ("muro", "arriostrado"):
                resistentes[tr["direccion"]] += 1
            tr.setdefault("niveles", {})[nivel] = dict(an, eleccion=eleccion, final=final, tipo_muro=tipo)
            dibujados = [(f["inicio"], f["fin"]) for f in cfg.proyecto.get("marcos_usuario") or [] if f.get("nivel") == nivel]
            if final in SISTEMA and sup and _cubierto(tr, dibujados):
                tr["niveles"][nivel]["motivo"] += " · el marco lo define el marco dibujado en la app"
                continue                                  # el marco dibujado reemplaza al del tramo
            if final in SISTEMA and sup:
                lego = _lego_por_sistema(cfg, SISTEMA[final])
                if lego is None:
                    advertencias.append(f"Tramo {tr['id']}: no hay lego de marco {final} en el catálogo")
                    continue
                nuevos.append((Segmento(tuple(tr["inicio"]), tuple(tr["fin"]), f"tramo {tr['id']}"), lego,
                               f"tramo {tr['id']}"))
        if sup:
            for d, n in resistentes.items():
                if n == 0:
                    advertencias.append(f"{nivel}: la dirección {d} no tiene muros ni marcos arriostrados; "
                                        "solo marcos de momento resisten en esa dirección")
        vistas = {(round(c["punto"][0], 3), round(c["punto"][1], 3))
                  for f in modelo["marcos"] if f["nivel"] == nivel for c in f["columnas"]}
        n0 = 1 + sum(1 for f in modelo["marcos"] if f["nivel"] == nivel)
        cub_nivel = next((l["nivel"] for l in modelo["losas"] if l.get("cubierta")), None)
        planos = marcos_mod.planos_de_cubierta(modelo.get("cubierta")) if sup and sup == cub_nivel else []
        modelo["marcos"] += marcos_mod.generar(nuevos, cfg, nivel, sup, cota, nivel, advertencias, planos_techo=planos,
                                               columnas_vistas=vistas, numero_inicial=n0)
    for tr in tramos:
        tr.pop("u", None)
    return tramos
