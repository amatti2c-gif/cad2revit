"""Modelo 3D para análisis y diseño (pestaña 6, modo 3D): los elementos que se seleccionan en Revit con el botón
«Modelo 3D a análisis» (pilares, vigas y diagonales de acero, y muros), con la misma estructura que el modelo 2D de un
eje: muros de marco plataforma como láminas con la rigidez del Manual (Ec. 2.22), mallados en su propio plano con la
misma rutina del 2D; cargas NCh1537 / NCh432 / NCh433:2026; combinaciones NCh3171; análisis con PyNite (estático y
modal espectral en X e Y, CQC) y diseño de acero AISC 360-16 (flexión en los dos ejes) y de muros de madera.

Ejes: X e Y en planta, Z vertical (m, tonf). Barras de PyNite: con Z vertical, en una barra horizontal el eje local z
es vertical, así que la flexión por gravedad (eje fuerte) es en torno al eje local y (My, Iy = eje fuerte).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

from .diseno_acero import FY_A270ES, Perfil
from .eje_cargas import (A0_ZONA, SUELOS_DS61, ParametrosCargas, cmax, cp_muro_sotavento, espectro_ds61,
                         lr_techo, q_viento, reduccion_lr, sistema_sismico)
from .eje_pynite import (FACTOR_VERTICAL, G_ACC, VN_OSB_9_5_8D, _cqc, _reacciones, _unpartition_rapido)
from .eje_revit import ACERO, ParametrosMuroMadera, _seccion, mallar

VERSION_3D = 1
MASA_OBJETIVO = 0.90
MODOS_MAX = 400
TOL = 0.003                     # m: nudos a menos de esta distancia son el mismo
TOL_UNION = 0.10                # m: barras que pasan a menos de esto se unen (excentricidades de modelación en Revit)


@dataclass
class Modelo3D:
    nombre: str
    nudos: dict = field(default_factory=dict)          # nombre → (x, y, z)
    barras: list = field(default_factory=list)         # (nombre, ni, nj, seccion)
    giro: dict = field(default_factory=dict)           # barra → giro de la sección (°)
    areas: list = field(default_factory=list)          # (nombre, [nudos], seccion_area)
    apoyos: dict = field(default_factory=dict)         # nudo → "empotrado" | "rotula" | "deslizante" | "x" (horizontal)
    apoyos_defecto: dict = field(default_factory=dict)
    materiales: dict = field(default_factory=dict)
    secciones: dict = field(default_factory=dict)
    secciones_area: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)          # barra → pilar | viga | techo | diagonal
    origen: dict = field(default_factory=dict)         # barra / área → id de Revit
    liberaciones: dict = field(default_factory=dict)   # barra → (rótula i, rótula j)
    l_fuera: dict = field(default_factory=dict)
    resumen_muros: list = field(default_factory=list)  # con el marco del muro: a (x, y), u (ux, uy)
    normal_area: dict = field(default_factory=dict)    # área → normal horizontal del muro (nx, ny)
    avisos: list = field(default_factory=list)
    sin_apoyo: list = field(default_factory=list)
    excluidos_auto: list = field(default_factory=list)
    dir_costaneras: list = field(default_factory=list)  # direcciones en planta (ux, uy) de las costaneras excluidas
    origen_xy: tuple = (0.0, 0.0)


# ------------------------------------------------------------------------------------------------- malla
T_TUBO_DEFECTO = 0.005          # m: espesor de un tubo circular cuando Revit no lo da


def _seccion3d(m: dict, s: dict, avisos: list, cambios: dict | None = None) -> str:
    """Como _seccion del 2D, más tubos circulares («D240mm», «Ø140», dims D y t) y cajones cuyas medidas vienen en
    el nombre («300x100x3»): si las dimensiones de la familia no calzan con el nombre, manda el nombre."""
    nombre = (m.get("seccion") or "SIN_NOMBRE").replace(" ", "_")
    if nombre in s:
        return nombre
    d = dict(m.get("dims") or {})
    cam = (cambios or {}).get(nombre) or {}
    txt = str(m.get("seccion") or "") + " " + str(m.get("familia") or "")
    circ = re.search(r"(?:\bD|Ø|ø|diam\w*\s*)\s*(\d+(?:[.,]\d+)?)\s*(mm)?", txt, re.I) if re.search(r"circ|tubo|pipe|Ø|ø|\bD\d", txt, re.I) else None
    caja = re.search(r"(\d+(?:[.,]\d+)?)\s*[xX×]\s*(\d+(?:[.,]\d+)?)\s*[xX×]\s*(\d+(?:[.,]\d+)?)(?!\s*[xX×])", txt)
    mm = lambda v: float(str(v).replace(",", ".")) / 1000                                    # noqa: E731
    if cam.get("forma") == "Pipe" or ("D" in cam) or (circ and not cam.get("forma")):
        D = cam.get("D") or cam.get("d") or d.get("D") or (mm(circ.group(1)) if circ else d.get("d", 0.1))
        t = cam.get("t") or d.get("t")
        if not t:
            t = T_TUBO_DEFECTO
            avisos.append(f"Sección {nombre}: tubo circular Ø{D * 1000:.0f} mm; Revit no dio el espesor, se usó "
                          f"{t * 1000:.0f} mm (cámbialo en «Secciones de acero»)")
        s[nombre] = {"forma": "Pipe", "material": "ACERO", "d": float(D), "bf": float(D), "t": float(t)}
        return nombre
    if cam.get("forma") == "Box" and all(cam.get(k) for k in ("d", "bf", "t")):
        s[nombre] = {"forma": "Box", "material": "ACERO", **{k: float(cam[k]) for k in ("d", "bf", "t")}}
        return nombre
    if cam.get("forma") == "I" and all(cam.get(k) for k in ("d", "bf", "tw", "tf")):
        s[nombre] = {"forma": "I", "material": "ACERO", **{k: float(cam[k]) for k in ("d", "bf", "tw", "tf")}}
        return nombre
    if caja and not cam and not all(k in d for k in ("tw", "tf")):
        a, b, t = (mm(caja.group(i)) for i in (1, 2, 3))
        if not ("d" in d and "bf" in d and "t" in d) or abs(d["d"] - a) > 0.2 * a or abs(d["bf"] - b) > 0.2 * b:
            if "d" in d:
                avisos.append(f"Sección {nombre}: las dimensiones de la familia (d {d.get('d', 0) * 1000:.0f}, b "
                              f"{d.get('bf', 0) * 1000:.0f} mm) no calzan con el nombre; se usó el nombre: cajón "
                              f"{a * 1000:.0f}x{b * 1000:.0f}x{t * 1000:g}")
            s[nombre] = {"forma": "Box", "material": "ACERO", "d": a, "bf": b, "t": t}
            return nombre
    return _seccion(m, s, avisos, cambios)


def _mas_cercanos(a0, da, b0, db):
    """Parámetros (ta, tb) en [0, 1] de los puntos más cercanos entre dos segmentos a0 + ta·da y b0 + tb·db."""
    r_ = a0 - b0
    A, B, C, D, E = da @ da, da @ db, db @ db, da @ r_, db @ r_
    den = A * C - B * B
    ta = min(max((B * E - C * D) / den, 0.0), 1.0) if den > 1e-12 else 0.0
    tb = (B * ta + E) / C
    if tb < 0 or tb > 1:
        tb = min(max(tb, 0.0), 1.0)
        ta = min(max((B * tb - D) / A, 0.0), 1.0)
    return float(ta), float(tb)


def _clave(p):
    return (round(p[0] / TOL), round(p[1] / TOL), round(p[2] / TOL))


def mallar3d(datos: dict, pm: ParametrosMuroMadera | None = None, *, malla: float = 0.5, apoyo_pilares: str = "rotula",
             opciones: dict | None = None) -> Modelo3D:
    pm = pm or ParametrosMuroMadera()
    op = opciones or {}
    excluir = {str(x) for x in op.get("excluir") or []}
    mod = Modelo3D(nombre=datos.get("nombre") or "Modelo 3D")
    mod.materiales["ACERO"] = dict(ACERO)
    # origen: esquina de los elementos en planta (números chicos)
    xs_ = [p[0] for e in (datos.get("pilares") or []) + (datos.get("vigas") or []) for p in (e["p0"], e["p1"])]
    ys_ = [p[1] for e in (datos.get("pilares") or []) + (datos.get("vigas") or []) for p in (e["p0"], e["p1"])]
    xs_ += [p[0] for w in datos.get("muros") or [] for p in (w["a"], w["b"])]
    ys_ += [p[1] for w in datos.get("muros") or [] for p in (w["a"], w["b"])]
    ox, oy = (min(xs_), min(ys_)) if xs_ else (0.0, 0.0)
    mod.origen_xy = (ox, oy)
    om = datos.get("omitidos") or []
    if om:
        mod.avisos.append("Revit no exportó " + "; ".join(f"{o.get('n')} {o.get('motivo')}" for o in om))
    P = lambda p: (float(p[0]) - ox, float(p[1]) - oy, float(p[2]))                       # noqa: E731

    pilares = []
    vigas = []
    for c in datos.get("pilares") or []:
        if str(c.get("id")) in excluir or c.get("material") in ("madera", "hormigon"):
            continue
        pilares.append({**c, "p0": P(c["p0"]), "p1": P(c["p1"])})
    for v in datos.get("vigas") or []:
        if str(v.get("id")) in excluir or v.get("material") in ("madera", "hormigon"):
            continue
        if v.get("costanera") and not op.get("incluir_costaneras"):
            mod.excluidos_auto.append(str(v.get("id")))
            dx_, dy_ = v["p1"][0] - v["p0"][0], v["p1"][1] - v["p0"][1]
            if math.hypot(dx_, dy_) > 0.1:
                mod.dir_costaneras.append((dx_ / math.hypot(dx_, dy_), dy_ / math.hypot(dx_, dy_)))
            continue
        vigas.append({**v, "p0": P(v["p0"]), "p1": P(v["p1"])})
    if mod.excluidos_auto:
        mod.avisos.append(f"{len(mod.excluidos_auto)} costanera(s) de sistemas de vigas no se modelan (se verifican en la pestaña 8)")
    # extremos de vigas junto a un pilar (≤ 0,40 m en planta, dentro de su altura): al eje del pilar
    for v in vigas:
        for k in ("p0", "p1"):
            x, y, z = v[k]
            for c in pilares:
                cx, cy = c["p0"][0], c["p0"][1]
                if math.hypot(x - cx, y - cy) <= 0.40 and min(c["p0"][2], c["p1"][2]) - 0.4 <= z <= max(c["p0"][2], c["p1"][2]) + 0.4:
                    v[k] = (cx, cy, z)
                    break
    # pilar que no llega por poco a una viga que pasa sobre él (≤ 0,30 m en altura, ≤ 0,10 m en planta, p. ej. por la
    # justificación de la viga en Revit): su cabeza se lleva al punto de la viga sobre su eje y la viga se parte ahí
    unidos = []
    for c in pilares:
        cx, cy, ztop = c["p1"]
        mejor = None
        for v in vigas:
            (x0, y0, z0), (x1, y1, z1) = v["p0"], v["p1"]
            L2 = (x1 - x0) ** 2 + (y1 - y0) ** 2
            if L2 < 1e-9:
                continue
            t = ((cx - x0) * (x1 - x0) + (cy - y0) * (y1 - y0)) / L2
            if not (-0.02 <= t <= 1.02):
                continue
            t = min(max(t, 0.0), 1.0)
            px, py = x0 + t * (x1 - x0), y0 + t * (y1 - y0)
            dp = math.hypot(px - cx, py - cy)
            if dp > TOL_UNION:
                continue
            zv = z0 + t * (z1 - z0)
            if abs(zv - ztop) <= 0.30 and (mejor is None or abs(zv - ztop) + dp < mejor[0]):
                mejor = (abs(zv - ztop) + dp, (px, py, zv), v)
        if mejor is not None:
            q, v = mejor[1], mejor[2]
            if math.dist(q, c["p1"]) > 0.005:
                unidos.append((str(c.get("id")), abs(q[2] - ztop), math.hypot(q[0] - cx, q[1] - cy)))
            c["p1"] = q
            v.setdefault("cortes", []).append(q)
    if unidos:
        mod.avisos.append(f"{len(unidos)} pilar(es) cuya cabeza no tocaba la viga en Revit se unieron a ella (separación hasta "
                          f"{1000 * max(u[1] for u in unidos):.0f} mm en altura y {1000 * max(u[2] for u in unidos):.0f} mm en "
                          f"planta, p. ej. por la justificación de la viga): " + ", ".join(u[0] for u in unidos[:15]) +
                          ("…" if len(unidos) > 15 else ""))
    # vigas que se cruzan o se tocan a menos de 0,10 m (extremos que no llegan justo, uniones excéntricas en Revit):
    # se unen en un nudo común; las cabezas de pilar no se mueven
    cabezas = [c["p1"] for c in pilares]
    fijo = lambda p_: any(math.dist(p_, q) < 0.005 for q in cabezas)                         # noqa: E731
    n_uniones = 0
    for ia in range(len(vigas)):
        for ib in range(ia + 1, len(vigas)):
            A, B = vigas[ia], vigas[ib]
            a0, a1, b0, b1 = (np.array(A["p0"], float), np.array(A["p1"], float), np.array(B["p0"], float), np.array(B["p1"], float))
            da, db = a1 - a0, b1 - b0
            La, Lb = float(np.linalg.norm(da)), float(np.linalg.norm(db))
            if La < 1e-3 or Lb < 1e-3 or abs(float(da @ db)) / (La * Lb) > 0.995:
                continue
            ta, tb = _mas_cercanos(a0, da, b0, db)
            pa, pb = a0 + ta * da, b0 + tb * db
            if float(np.linalg.norm(pa - pb)) > TOL_UNION:
                continue
            ext_a = "p0" if ta * La <= TOL_UNION else ("p1" if (1 - ta) * La <= TOL_UNION else None)
            ext_b = "p0" if tb * Lb <= TOL_UNION else ("p1" if (1 - tb) * Lb <= TOL_UNION else None)
            if ext_a and ext_b:
                q = A[ext_a] if fijo(A[ext_a]) else (B[ext_b] if fijo(B[ext_b]) else tuple(pa))
            elif ext_b:
                q = B[ext_b] if fijo(B[ext_b]) else tuple(pa)
            elif ext_a:
                q = A[ext_a] if fijo(A[ext_a]) else tuple(pb)
            else:
                q = tuple(pa)
            q = tuple(float(x) for x in q)
            for V_, ext in ((A, ext_a), (B, ext_b)):
                if ext and not fijo(V_[ext]):
                    V_[ext] = q
                else:
                    V_.setdefault("cortes", []).append(q)
            n_uniones += 1
    if n_uniones:
        mod.avisos.append(f"{n_uniones} unión(es) entre vigas que en Revit no se tocan exactamente (≤ {TOL_UNION * 100:.0f} cm): "
                          "se unieron en un nudo común")

    claves: dict = {}

    def nudo(p):
        k = _clave(p)
        if k not in claves:
            n = f"N{len(claves) + 1}"
            claves[k] = n
            mod.nudos[n] = (round(p[0], 4), round(p[1], 4), round(p[2], 4))
        return claves[k]

    # ---- muros: cada uno se malla en su plano con la rutina del 2D (con los pilares y vigas de su plano) ----
    for w in datos.get("muros") or []:
        if str(w.get("id")) in excluir or w.get("material") == "hormigon":
            continue
        ax_, ay_ = float(w["a"][0]) - ox, float(w["a"][1]) - oy
        bx_, by_ = float(w["b"][0]) - ox, float(w["b"][1]) - oy
        L = math.hypot(bx_ - ax_, by_ - ay_)
        if L < 0.2:
            continue
        ux, uy = (bx_ - ax_) / L, (by_ - ay_) / L
        tpos = lambda x, y: (x - ax_) * ux + (y - ay_) * uy          # noqa: E731
        dist = lambda x, y: abs((x - ax_) * uy - (y - ay_) * ux)      # noqa: E731
        en_plano_p = [c for c in pilares if dist(c["p0"][0], c["p0"][1]) <= 0.30 and -0.4 <= tpos(c["p0"][0], c["p0"][1]) <= L + 0.4]
        en_plano_v = [v for v in vigas if dist(v["p0"][0], v["p0"][1]) <= 0.30 and dist(v["p1"][0], v["p1"][1]) <= 0.30]
        eje = {
            "eje": f"muro {w.get('id')}",
            "pilares": [{"id": c["id"], "seccion": c["seccion"], "material": "acero", "x0": tpos(*c["p0"][:2]),
                         "x1": tpos(*c["p1"][:2]), "z0": c["p0"][2], "z1": c["p1"][2], "dims": c.get("dims") or {}}
                        for c in en_plano_p],
            "vigas": [{"id": v["id"], "seccion": v["seccion"], "material": "acero", "tipo": v.get("tipo", "viga"),
                       "x0": tpos(*v["p0"][:2]), "z0": v["p0"][2], "x1": tpos(*v["p1"][:2]), "z1": v["p1"][2],
                       "dims": v.get("dims") or {}} for v in en_plano_v],
            "muros": [{"id": w["id"], "tipo": w.get("tipo", ""), "material": w.get("material", "madera"), "x0": 0.0, "x1": L,
                       "z0": w["z0"], "espesor": w.get("espesor", 0.1), "nucleo": w.get("nucleo", w.get("espesor", 0.1)),
                       "perfil": w.get("perfil") or [[0, w["z0"] + 2.4], [L, w["z0"] + 2.4]],
                       "vanos": [{"x0": v["t0"], "x1": v["t1"], "z0": v["z0"], "z1": v["z1"], "puerta": v.get("puerta")}
                                 for v in w.get("vanos") or []]}],
        }
        m2 = mallar(eje, pm, malla=malla, apoyo_pilares=apoyo_pilares,
                    opciones={"muros": op.get("muros") or {}, "recortar_pilares": False})
        a3 = lambda n_: (ax_ + ux * m2.nudos[n_][0], ay_ + uy * m2.nudos[n_][0], m2.nudos[n_][1])   # noqa: E731
        nudos_areas = set()
        for nombre, ns, sec in m2.areas:
            nn = [nudo(a3(n_)) for n_ in ns]
            nombre3 = f"M{w['id']}_{nombre}"
            mod.areas.append((nombre3, nn, sec))
            mod.origen[nombre3] = str(w["id"])
            mod.normal_area[nombre3] = (uy, -ux)
            nudos_areas.update(ns)
        for sec, d in m2.secciones_area.items():
            mod.secciones_area[sec] = d
        for k, d in m2.materiales.items():
            mod.materiales.setdefault(k, d)
        # apoyos de la base del muro (los de los pilares se ponen después, con los pilares)
        pil_x = {round(tpos(*c["p0"][:2]), 3) for c in en_plano_p}
        for n_, t_ in m2.apoyos.items():
            if n_ in nudos_areas and round(m2.nudos[n_][0], 3) not in pil_x:
                mod.apoyos[nudo(a3(n_))] = t_
        for seg in m2.resumen_muros:
            mod.resumen_muros.append({**seg, "a": (ax_, ay_), "u": (ux, uy)})

    # ---- barras: pilares y vigas, partidas en todos los nudos que caen sobre ellas ----
    for c in pilares:
        nudo(c["p0"]); nudo(c["p1"])
    for v in vigas:
        nudo(v["p0"]); nudo(v["p1"])
        for q in v.get("cortes") or ():
            nudo(q)
    coords = {n: np.array(p) for n, p in mod.nudos.items()}

    def barra(p0, p1, sec, rol, origen, giro, cortes=()):
        p0, p1 = np.array(p0, float), np.array(p1, float)
        d = p1 - p0
        L = float(np.linalg.norm(d))
        if L < 1e-3:
            return
        u = d / L
        sobre = []
        for n, q in coords.items():
            w_ = q - p0
            t = float(w_ @ u)
            if 1e-3 < t < L - 1e-3 and np.linalg.norm(w_ - t * u) < 2 * TOL:
                sobre.append((t, n))
        for q in cortes:                                  # uniones que quedan levemente fuera del eje de la barra
            t = float((np.array(q) - p0) @ u)
            if 1e-3 < t < L - 1e-3:
                sobre.append((t, nudo(q)))
        sobre = sorted(dict((n, t) for t, n in sorted(sobre)).items(), key=lambda x: x[1])
        sobre = [(t, n) for n, t in sobre]
        cadena = [nudo(p0)] + [n for _, n in sorted(sobre)] + [nudo(p1)]
        for a, b in zip(cadena, cadena[1:]):
            if a == b:
                continue
            nombre = f"F{len(mod.barras) + 1}"
            mod.barras.append((nombre, a, b, sec))
            mod.roles[nombre] = rol
            mod.origen[nombre] = origen
            mod.giro[nombre] = giro
    for c in pilares:
        sec = _seccion3d(c, mod.secciones, mod.avisos, op.get("secciones"))
        barra(c["p0"], c["p1"], sec, "pilar", str(c["id"]), float(c.get("giro") or 0))
        base = c["p0"] if c["p0"][2] <= c["p1"][2] else c["p1"]
        mod.apoyos[nudo(base)] = (op.get("apoyos") or {}).get(str(c["id"]), apoyo_pilares)
    # techo: vigas inclinadas o sin otra viga encima (reciben el peso del techo, la sobrecarga y el viento)
    def encima(v):
        (x0, y0, z0), (x1, y1, z1) = v["p0"], v["p1"]
        xm, ym, zm = (x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2
        for o in vigas:
            if o is v:
                continue
            (a0, b0, c0), (a1, b1, c1) = o["p0"], o["p1"]
            L2 = (a1 - a0) ** 2 + (b1 - b0) ** 2
            if L2 < 1e-9:
                continue
            t = ((xm - a0) * (a1 - a0) + (ym - b0) * (b1 - b0)) / L2
            if 0 <= t <= 1 and math.hypot(a0 + t * (a1 - a0) - xm, b0 + t * (b1 - b0) - ym) < 0.5 and c0 + t * (c1 - c0) > zm + 0.3:
                return True
        return False
    for v in vigas:
        rol = "diagonal" if v.get("tipo") == "diagonal" else (
            "techo" if abs(v["p1"][2] - v["p0"][2]) > 0.05 or not encima(v) else "viga")
        sec = _seccion3d(v, mod.secciones, mod.avisos, op.get("secciones"))
        barra(v["p0"], v["p1"], sec, rol, str(v["id"]), float(v.get("giro") or 0), v.get("cortes") or ())

    # ---- voladizos: extremos de viga que no llegan a nada (la línea de Revit puede ser más larga que la viga) ----
    grado_n: dict = {}
    for _, a_, b_, _ in mod.barras:
        grado_n[a_] = grado_n.get(a_, 0) + 1
        grado_n[b_] = grado_n.get(b_, 0) + 1
    largos = []
    for v in vigas:
        segs = [b for b in mod.barras if mod.origen.get(b[0]) == str(v["id"])]
        for k in ("p0", "p1"):
            n_ = claves.get(_clave(v[k]))
            if n_ is None or grado_n.get(n_, 0) != 1 or n_ in mod.apoyos:
                continue
            seg = next((b for b in segs if n_ in b[1:3]), None)
            if seg is not None:
                L_ = math.dist(mod.nudos[seg[1]], mod.nudos[seg[2]])
                if L_ > 1.5:
                    largos.append((str(v["id"]), L_))
    if largos:
        mod.avisos.append(f"{len(largos)} viga(s) con un voladizo libre de más de 1,5 m (hasta {max(l for _, l in largos):.2f} m): "
                          + ", ".join(i for i, _ in largos[:12]) + ("…" if len(largos) > 12 else "") +
                          ". Si en Revit la viga es más corta o llega a un pilar, vuelve a exportar con el complemento al día "
                          "(recorta la viga a su geometría) o incluye ese pilar")

    # ---- apoyos y uniones cambiados a mano ----
    mod.apoyos_defecto = dict(mod.apoyos)
    for clave, tipo in (op.get("apoyos_nudo") or {}).items():
        try:
            q = [float(c) for c in str(clave).split(",")]
        except ValueError:
            continue
        if len(q) != 3:
            continue
        n_ = next((n for n, p in mod.nudos.items() if max(abs(p[i] - q[i]) for i in range(3)) < 2e-3), None)
        if n_ is None:
            continue
        if tipo == "libre":
            mod.apoyos.pop(n_, None)
        elif tipo in ("empotrado", "rotula", "deslizante", "x"):
            mod.apoyos[n_] = tipo
    for v in vigas:
        u = (op.get("uniones") or {}).get(str(v["id"])) or {}
        segs = [b for b in mod.barras if mod.origen.get(b[0]) == str(v["id"])]
        for lado, p in (("i", v["p0"]), ("j", v["p1"])):
            if u.get(lado) != "rotula":
                continue
            n_p = claves.get(_clave(p))
            for nb, a_, b_, _ in segs:
                for k_, n_ in ((0, a_), (1, b_)):
                    if n_ == n_p:
                        li = list(mod.liberaciones.get(nb, (False, False)))
                        li[k_] = True
                        mod.liberaciones[nb] = tuple(li)
    mod.l_fuera = {str(k): v for k, v in (op.get("l_fuera") or {}).items() if v not in (None, "")}

    # ---- estabilidad: se sacan los elementos sin camino a un apoyo y los pilares rotulados sueltos arriba ----
    vecinos: dict = {}
    for _, a_, b_, _ in mod.barras:
        vecinos.setdefault(a_, set()).add(b_)
        vecinos.setdefault(b_, set()).add(a_)
    for _, ns, _ in mod.areas:
        for n_ in ns:
            vecinos.setdefault(n_, set()).update(ns)
    alcanzados, pila = set(mod.apoyos), list(mod.apoyos)
    while pila:
        n_ = pila.pop()
        for m_ in vecinos.get(n_, ()):
            if m_ not in alcanzados:
                alcanzados.add(m_)
                pila.append(m_)
    grado: dict = {}
    for _, a_, b_, _ in mod.barras:
        grado[a_] = grado.get(a_, 0) + 1
        grado[b_] = grado.get(b_, 0) + 1
    en_area = {n_ for _, ns, _ in mod.areas for n_ in ns}
    for nb, a_, b_, _ in list(mod.barras):
        if mod.roles.get(nb) != "pilar":
            continue
        base, cab = (a_, b_) if mod.nudos[a_][2] <= mod.nudos[b_][2] else (b_, a_)
        if mod.apoyos.get(base) in ("rotula", "deslizante", "x") and grado.get(cab, 0) == 1 and cab not in en_area:
            alcanzados.discard(cab)
            alcanzados.discard(base)
    sueltas = [b for b in mod.barras if b[1] not in alcanzados or b[2] not in alcanzados]
    sueltas_a = [a for a in mod.areas if a[1][0] not in alcanzados]
    mod.sin_apoyo = sorted({mod.origen.get(x[0]) for x in sueltas + sueltas_a if mod.origen.get(x[0])})
    if mod.sin_apoyo:
        mod.barras = [b for b in mod.barras if b[1] in alcanzados and b[2] in alcanzados]
        mod.areas = [a for a in mod.areas if a[1][0] in alcanzados]
        usados = {n_ for b in mod.barras for n_ in b[1:3]} | {n_ for a in mod.areas for n_ in a[1]}
        mod.nudos = {k: v for k, v in mod.nudos.items() if k in usados}
        mod.apoyos = {k: v for k, v in mod.apoyos.items() if k in usados}
        mod.avisos.append("Sin apoyo o inestables (no se modelan): " + ", ".join(mod.sin_apoyo))
    if not mod.barras and not mod.areas:
        mod.avisos.append("El modelo no tiene elementos para analizar")
    return mod


# ------------------------------------------------------------------------------------------------- cargas
@dataclass
class Cargas3D:
    patrones: dict = field(default_factory=dict)       # nombre → tipo
    barras: list = field(default_factory=list)         # (barra, patrón, (fx, fy, fz) tonf/m global, por largo de barra)
    nudos: list = field(default_factory=list)          # (nudo, patrón, (fx, fy, fz) tonf)
    combos: dict = field(default_factory=dict)
    resumen: list = field(default_factory=list)
    trib: dict = field(default_factory=dict)           # barra de techo → ancho tributario (m)


def _vigas_que_cargan(mod: Modelo3D, techo: list) -> tuple[list, str]:
    """El techo se apoya en las vigas de una sola dirección (las que reciben las costaneras), para no contar dos veces su
    peso: perpendiculares a las costaneras si se conocen; si no, las de la dirección de las vigas inclinadas (tijerales);
    si no, las paralelas al lado corto de la planta (los marcos)."""
    def dirs(f):
        _, a, b, _ = next(x for x in mod.barras if x[0] == f)
        (x0, y0, z0), (x1, y1, z1) = mod.nudos[a], mod.nudos[b]
        h = math.hypot(x1 - x0, y1 - y0)
        return (np.array([x1 - x0, y1 - y0]) / h if h > 1e-6 else None), abs(z1 - z0) > 0.05
    info = {f: dirs(f) for f in techo}
    if mod.dir_costaneras:
        uc = np.array(mod.dir_costaneras[0])
        return [f for f, (u, _) in info.items() if u is not None and abs(u @ uc) < 0.5], "perpendiculares a las costaneras"
    incl = [u for u, i in info.values() if i and u is not None]
    if incl:
        uc = incl[0]
        return [f for f, (u, _) in info.items() if u is not None and abs(u @ uc) > 0.866], "en la dirección de las vigas inclinadas"
    xy = np.array([q[:2] for q in mod.nudos.values()])
    uc = np.array([1.0, 0.0]) if np.ptp(xy[:, 0]) <= np.ptp(xy[:, 1]) else np.array([0.0, 1.0])
    return [f for f, (u, _) in info.items() if u is not None and abs(u @ uc) > 0.866], "paralelas al lado corto de la planta"


def _ancho_tributario(mod: Modelo3D, techo: list, defecto: float) -> dict:
    """Ancho tributario de cada barra de techo: la mitad de la distancia en planta a las vigas de techo paralelas
    vecinas a cada lado (que se traslapan); si solo hay a un lado, la mitad de esa; si no hay, el ancho dado."""
    seg = {}
    for f in techo:
        _, a, b, _ = next(x for x in mod.barras if x[0] == f)
        seg[f] = (np.array(mod.nudos[a][:2]), np.array(mod.nudos[b][:2]))
    out = {}
    for f, (p0, p1) in seg.items():
        d = p1 - p0
        L = float(np.linalg.norm(d))
        if L < 1e-6:
            out[f] = defecto
            continue
        u = d / L
        nrm = np.array([-u[1], u[0]])
        lados = {1: [], -1: []}
        for g, (q0, q1) in seg.items():
            if g == f or mod.origen.get(g) == mod.origen.get(f):
                continue
            e = q1 - q0
            le = float(np.linalg.norm(e))
            if le < 1e-6 or abs(float(e @ u) / le) < 0.98:
                continue
            ta, tb = sorted((float((q0 - p0) @ u), float((q1 - p0) @ u)))
            if tb < 0.05 * L or ta > 0.95 * L:
                continue
            dd = float(((q0 + q1) / 2 - p0) @ nrm)
            if abs(dd) > 0.05:
                lados[1 if dd > 0 else -1].append(abs(dd))
        d1, d2 = min(lados[1], default=None), min(lados[-1], default=None)
        if d1 is None and d2 is None:
            out[f] = defecto
        else:
            out[f] = min(((d1 or 0) + (d2 or 0)) / 2 if d1 and d2 else (d1 or d2) / 2, 10.0)
    return out


def cargas3d(mod: Modelo3D, p: ParametrosCargas) -> Cargas3D:
    c = Cargas3D()
    c.patrones = {"DEAD": "DEAD", "LR": "ROOF LIVE"}
    techo, criterio = _vigas_que_cargan(mod, [f for f, r in mod.roles.items() if r == "techo"])
    defecto = float(p.ancho_colaborante or 1.0)
    trib = _ancho_tributario(mod, techo, defecto) if not p.ancho_colaborante else {f: defecto for f in techo}
    c.trib = trib
    zs = [z for _, _, z in mod.nudos.values()] or [0.0]
    z_base = min(zs)
    xy = np.array([q[:2] for q in mod.nudos.values()]) if mod.nudos else np.zeros((1, 2))
    Bx, By = float(np.ptp(xy[:, 0])) or 1.0, float(np.ptp(xy[:, 1])) or 1.0
    c.resumen.append("Ancho tributario de las vigas de techo: " + ("dado en la app, " + f"{defecto:.2f} m" if p.ancho_colaborante else
                     "la mitad de la distancia a las vigas de techo paralelas vecinas (en planta)") +
                     f"; el techo se apoya en las vigas {criterio}" +
                     (f"; rango {min(trib.values()):.2f}–{max(trib.values()):.2f} m" if trib else ""))
    c.resumen.append(f"D techo: {p.peso_techo * 1000:.0f} kgf/m² × ancho tributario, sobre las vigas de techo")
    pendiente = {}
    for f in techo:
        _, a, b, _ = next(x for x in mod.barras if x[0] == f)
        (x0, y0, z0), (x1, y1, z1) = mod.nudos[a], mod.nudos[b]
        h = math.hypot(x1 - x0, y1 - y0)
        pendiente[f] = abs(z1 - z0) / h if h > 1e-6 else 0.0
        c.barras.append((f, "DEAD", (0.0, 0.0, -p.peso_techo * trib[f])))
        at = max(h, 1.0) * trib[f]
        lr = lr_techo(p, at, pendiente[f] * 100)
        cos = h / max(math.hypot(h, z1 - z0), 1e-9)
        c.barras.append((f, "LR", (0.0, 0.0, -lr * trib[f] * cos)))          # por m² de proyección horizontal
    if techo:
        red = reduccion_lr(max(trib.values()) * 3, max(pendiente.values()) * 100)
        c.resumen.append(f"Lr NCh1537: Lo {p.lo_techo * 1000:.0f} kgf/m²" + (" reducida por R1·R2 (§8.2) según el área tributaria y la "
                         f"pendiente de cada viga (p. ej. R2 = {red['R2']:.2f}), mínimo 0,3 kPa" if p.reducir_lr else ", sin reducir"))
    # ---- viento NCh432 (simplificado para el modelo 3D): muros exteriores (barlovento 0,8 y sotavento según L/B con
    # q_h; laterales −0,7) y succión en el techo (Cp −0,7 con GCpi +0,18), en ±X y ±Y ----
    h = max((max(zs) - z_base) * 0.85, 1.0)
    qh = q_viento(p, h)
    nudos_area = {}
    for nombre, ns, sec in mod.areas:
        nudos_area[nombre] = ns
    muros_info = {}
    for nombre, ns, _ in mod.areas:
        w = mod.origen.get(nombre)
        pts = np.array([mod.nudos[n] for n in ns])
        muros_info.setdefault(w, {"pts": [], "normal": mod.normal_area[nombre]})["pts"].append(pts)
    for wid, info in muros_info.items():
        allp = np.vstack(info["pts"])
        info["c"] = allp[:, :2].mean(axis=0)
    xmin, xmax, ymin, ymax = float(xy[:, 0].min()), float(xy[:, 0].max()), float(xy[:, 1].min()), float(xy[:, 1].max())
    casos = [("W1", np.array([1.0, 0.0])), ("W2", np.array([-1.0, 0.0])), ("W3", np.array([0.0, 1.0])), ("W4", np.array([0.0, -1.0]))]
    for nombre_w, dvec in casos:
        c.patrones[nombre_w] = "WIND"
        L_ = Bx if abs(dvec[0]) > 0.5 else By
        B_ = By if abs(dvec[0]) > 0.5 else Bx
        cp_sot = cp_muro_sotavento(L_ / B_)
        for wid, info in muros_info.items():
            nrm = np.array(info["normal"])
            cx_, cy_ = info["c"]
            borde_x = min(abs(cx_ - xmin), abs(cx_ - xmax)) < 0.6
            borde_y = min(abs(cy_ - ymin), abs(cy_ - ymax)) < 0.6
            if abs(nrm @ dvec) > 0.7:                                 # muro perpendicular al viento
                if not (borde_x if abs(dvec[0]) > 0.5 else borde_y):
                    continue                                         # muro interior
                barlov = (cx_ - (xmin if dvec[0] > 0 else xmax) if abs(dvec[0]) > 0.5 else cy_ - (ymin if dvec[1] > 0 else ymax))
                cp = p.cp_barlovento_muro if abs(barlov) < 0.6 else cp_sot
                sentido = dvec * (1 if cp > 0 else -1)                # presión empuja a sotavento; succión tira hacia afuera (también a sotavento)
                pres = abs(qh * p.g_rafaga * cp)
                f_dir = dvec if cp > 0 else dvec                      # barlovento empuja; sotavento succiona: ambos en +dvec
                del sentido
            else:
                if not (borde_y if abs(dvec[0]) > 0.5 else borde_x):
                    continue
                cp = -0.7
                pres = abs(qh * p.g_rafaga * cp)
                exterior = np.array([1.0, 0.0]) if abs(dvec[0]) <= 0.5 and cx_ > (xmin + xmax) / 2 else (
                    np.array([-1.0, 0.0]) if abs(dvec[0]) <= 0.5 else (np.array([0.0, 1.0]) if cy_ > (ymin + ymax) / 2 else np.array([0.0, -1.0])))
                f_dir = exterior                                     # succión: hacia afuera
            for nombre, ns, _ in mod.areas:
                if mod.origen.get(nombre) != wid:
                    continue
                pts = np.array([mod.nudos[n] for n in ns])
                # área del polígono en el plano del muro (coordenada horizontal a lo largo del muro y z)
                u = np.array([-info["normal"][1], info["normal"][0]])
                tt = pts[:, :2] @ u
                zz = pts[:, 2]
                area = 0.5 * abs(sum(tt[k] * zz[(k + 1) % len(ns)] - tt[(k + 1) % len(ns)] * zz[k] for k in range(len(ns))))
                F = pres * area / len(ns)
                for n in ns:
                    c.nudos.append((n, nombre_w, (F * f_dir[0], F * f_dir[1], 0.0)))
        for f in techo:
            pn = qh * (p.g_rafaga * -0.7 - p.gcpi)                    # succión (negativa: hacia arriba)
            c.barras.append((f, nombre_w, (0.0, 0.0, -pn * trib[f])))
    c.resumen.append(f"Viento NCh432 (simplificado 3D): q_h = {qh * 1000:.1f} kgf/m² (V {p.v_viento} m/s, exposición {p.exposicion}, "
                     f"h {h:.2f} m); muros exteriores: barlovento Cp {p.cp_barlovento_muro:.1f}, sotavento según L/B, laterales −0,7; "
                     "techo: succión Cp −0,7 con GCpi +0,18; W1 +X, W2 −X, W3 +Y, W4 −Y")
    sis = sistema_sismico(p)
    s_, t0_, tp_, n_, pp_ = SUELOS_DS61[p.suelo]
    c.resumen.append(f"Sismo NCh433:2026: zona {p.zona} (A0 {A0_ZONA[p.zona]:.2f} g), suelo {p.suelo} (S {s_}, T0 {t0_} s), "
                     f"I {p.importancia_sismo}; {sis['texto']}; análisis modal espectral en X y en Y (CQC), cada uno con su T*; "
                     "corte basal entre Qmín y Qmáx por dirección; vertical = 2/3 del horizontal")
    c.resumen.append("E de diseño = máx(EX + 0,3·EY + 0,3·EZ; 0,3·EX + EY + 0,3·EZ; 0,3·EX + 0,3·EY + EZ), en ambos sentidos")
    c.patrones["E"] = "QUAKE"
    c.combos["C1_1.4D"] = {"DEAD": 1.4}
    c.combos["C2_1.2D+0.5Lr"] = {"DEAD": 1.2, "LR": 0.5}
    for w in [n for n, t in c.patrones.items() if t == "WIND"]:
        c.combos[f"C3b_1.2D+1.6Lr+0.8{w}"] = {"DEAD": 1.2, "LR": 1.6, w: 0.8}
        c.combos[f"C4_1.2D+1.6{w}+0.5Lr"] = {"DEAD": 1.2, w: 1.6, "LR": 0.5}
        c.combos[f"C6_0.9D+1.6{w}"] = {"DEAD": 0.9, w: 1.6}
    c.combos["C5_1.2D+1.4E"] = {"DEAD": 1.2, "E": 1.4}
    c.combos["C7_0.9D+1.4E"] = {"DEAD": 0.9, "E": 1.4}
    return c


# ------------------------------------------------------------------------------------------------- análisis
def _capacidades3d(p: Perfil, g: dict, L_y: float, L_z: float, Lb: float, Fy: float) -> dict:
    """Capacidades con flexión en los dos ejes (AISC 360-16): eje fuerte con capacidades() del 2D; eje débil F6/F7."""
    from .diseno_acero import capacidades
    cap = capacidades(p, L_y, L_z, Lb, Fy=Fy)
    if p.forma == "Pipe":
        cap["phiMny"] = cap["phiMn"]
        return cap
    if p.forma == "Box":
        Zy = g.get("Zy") or g["Iy"] / (p.bf / 2)
        Mny = Fy * Zy
        lam = (p.d - 3 * p.t) / p.t
        raiz = math.sqrt(2.0e8 / 9.80665 / Fy)
        if lam > 1.12 * raiz:
            Mny *= max(0.6, 1.12 * raiz / lam)
    else:
        Sy = g["Iy"] / (p.bf / 2)
        Zy = p.tf * p.bf ** 2 / 2 + (p.d - 2 * p.tf) * p.tw ** 2 / 4
        Mny = min(Fy * Zy, 1.6 * Fy * Sy)
    cap["phiMny"] = 0.9 * Mny
    return cap


def _razon_biaxial(Pu, Mux, Muy, Vu, cap) -> dict:
    """AISC 360-16 H1-1 con flexión en los dos ejes (Pu + compresión)."""
    Pc = cap["phiPn"] if Pu >= 0 else cap["phiTn"]
    pr = abs(Pu) / Pc if Pc > 0 else 0.0
    m_ = Mux / cap["phiMn"] + Muy / cap["phiMny"]
    if pr >= 0.2:
        it, ec = pr + 8 / 9 * m_, "H1-1a"
    else:
        it, ec = pr / 2 + m_, "H1-1b"
    return {"interaccion": it, "corte": Vu / cap["phiVn"] if cap["phiVn"] else 0.0, "ecuacion": ec}


class ModeloInestable(ValueError):
    """El modelo tiene un mecanismo; el texto dice qué se mueve, hacia dónde y qué falta."""


def _mecanismo(m, mod: Modelo3D) -> str:
    """Modo de cuerpo rígido (valor propio ~0 de K11): dirección dominante y elementos de Revit que se mueven."""
    import scipy.sparse.linalg as sla
    from Pynite import Analysis
    try:
        combo = list(m.load_combos)[0]
        D1, _, _ = Analysis._partition_D(m)
        K11 = m.Ke(combo, False, False, True).tocsr()[D1, :][:, D1]
        n = K11.shape[0]
        if n <= 2500:
            w, V = np.linalg.eigh(K11.toarray())
            v = V[:, 0]
        else:
            w, V = sla.eigsh(K11.tocsc(), k=1, sigma=-1.0, which="LM")
            v = V[:, 0]
    except Exception:                                       # noqa: BLE001
        return ("El modelo 3D es inestable (matriz de rigidez singular): revise los apoyos y las uniones, o agregue "
                "arriostramiento (vigas, diagonales o muros) en ambas direcciones")
    ids = {nd.ID: nombre for nombre, nd in m.nodes.items()}
    mov: dict = {}
    for i, g in enumerate(D1):
        if abs(v[i]) > 0.2 * np.abs(v).max():
            mov.setdefault("DX DY DZ RX RY RZ".split()[g % 6], set()).add(ids[g // 6])
    gl = max(("DX", "DY", "DZ"), key=lambda k: len(mov.get(k, ())))
    nudos = mov.get(gl) or set().union(*mov.values())
    elems = sorted({mod.origen.get(b[0]) for b in mod.barras if b[1] in nudos or b[2] in nudos} |
                   {mod.origen.get(a[0]) for a in mod.areas if set(a[1]) & nudos} - {None})
    eje = {"DX": "X", "DY": "Y", "DZ": "vertical"}[gl]
    gira = [k for k in ("RX", "RY", "RZ") if mov.get(k)]
    txt = (f"El modelo 3D es inestable: hay un mecanismo en la dirección {eje}" +
           (f" (con giro {', '.join(gira)})" if gira else "") +
           f" que mueve {len(elems)} elemento(s) de Revit: {', '.join(elems[:12])}{'…' if len(elems) > 12 else ''}. ")
    if gl in ("DX", "DY"):
        txt += (f"Nada los amarra en {eje}: faltan vigas, diagonales o muros en esa dirección (¿se seleccionaron todos en "
                "Revit?), o use base empotrada en los pilares.")
    else:
        txt += "Revise que esos elementos lleguen a un apoyo o a otro elemento."
    return txt


def analizar3d(mod: Modelo3D, cargas: Cargas3D, pc: ParametrosCargas, *, fy: float = FY_A270ES, n_puntos: int = 7,
               avance=None, limites: dict | None = None) -> dict:
    from Pynite import Analysis, FEModel3D
    import scipy.sparse as sps
    import scipy.sparse.linalg as sla

    def paso(pct, etapa, hasta=None):
        if avance:
            avance(round(pct), etapa, round(hasta if hasta is not None else pct))
    paso(2, "Armando el modelo 3D en PyNite", 5)
    m = FEModel3D()
    for n, (x, y, z) in mod.nudos.items():
        m.add_node(n, x, y, z)
    for nombre, d in mod.materiales.items():
        m.add_material(nombre, d["E"], d["E"] / (2 * (1 + d["nu"])), d["nu"], 0.0)
    mat_area = {}
    for sec, a in mod.secciones_area.items():
        base = mod.materiales[a["material"]]
        E0, G0 = base["E"], base["E"] / (2 * (1 + base["nu"]))
        E_cal, G_cal = E0 * a["f11"], G0 * a["f12"]
        m.add_material(f"MAT_{sec}", 2 * G_cal, G_cal, 0.0, 0.0)
        mat_area[sec] = (f"MAT_{sec}", E_cal / (2 * G_cal))
    perfiles = {}
    for sec, s in mod.secciones.items():
        p = Perfil(s["forma"], s["d"], s["bf"], s.get("tw", 0.0), s.get("tf", 0.0), s.get("t", 0.0))
        g = p.props()
        perfiles[sec] = (p, g)
        m.add_section(sec, g["A"], g["Ix"], g["Iy"], g["J"])          # 3D (Z vertical): Iy local = eje fuerte
    for f, a, b, sec in mod.barras:
        m.add_member(f, a, b, "ACERO", sec, rotation=float(mod.giro.get(f, 0.0)))
    for f, (ri, rj) in mod.liberaciones.items():
        m.def_releases(f, Ryi=bool(ri), Rzi=bool(ri), Ryj=bool(rj), Rzj=bool(rj))
    sub = {}
    extra = {}

    def nudo_xyz(p):
        k = tuple(round(v, 6) for v in p)
        if k not in extra:
            extra[k] = f"_P{len(extra) + 1}"
            m.add_node(extra[k], *p)
        return extra[k]
    for nombre, ns, sec in mod.areas:
        t_ = mod.secciones_area[sec]["espesor"]
        if len(ns) == 4:
            m.add_quad(nombre, *ns, t_, mat_area[sec][0], mat_area[sec][1], mat_area[sec][1])
            sub[nombre] = [nombre]
            continue
        Pt = [np.array(mod.nudos[n]) for n in ns]
        g_ = nudo_xyz(tuple(sum(Pt) / 3))
        med = [nudo_xyz(tuple((Pt[k] + Pt[(k + 1) % 3]) / 2)) for k in range(3)]
        sub[nombre] = []
        for k in range(3):
            q = f"{nombre}_{k + 1}"
            m.add_quad(q, ns[k], med[k], g_, med[k - 1], t_, mat_area[sec][0], mat_area[sec][1], mat_area[sec][1])
            sub[nombre].append(q)
    # apoyos; en los nudos solo de muros se fija el giro en torno a la normal del muro (las láminas de PyNite no
    # tienen rigidez de «perforación»); en nudos a los que todas las barras llegan rotuladas, los giros
    con_barra = {x for _, a, b, _ in mod.barras for x in (a, b)}
    rigidas = {}
    for f, a, b, _ in mod.barras:
        ri, rj = mod.liberaciones.get(f, (False, False))
        rigidas[a] = rigidas.get(a, 0) + (0 if ri else 1)
        rigidas[b] = rigidas.get(b, 0) + (0 if rj else 1)
    en_quad = {}
    for nombre, ns, _ in mod.areas:
        for n in ns:
            en_quad.setdefault(n, set()).add(tuple(round(v, 3) for v in mod.normal_area[nombre]))
    for nombre in sub:
        for q in sub[nombre]:
            for nn in (m.quads[q].i_node.name, m.quads[q].j_node.name, m.quads[q].m_node.name, m.quads[q].n_node.name):
                if nn.startswith("_P"):
                    en_quad.setdefault(nn, set()).add(tuple(round(v, 3) for v in mod.normal_area[nombre]))
    for n in list(mod.nudos) + list(extra.values()):
        t = mod.apoyos.get(n)
        dx = dy = t in ("rotula", "empotrado", "x")
        dz = t in ("rotula", "empotrado", "deslizante")
        rx = ry = rz = t == "empotrado"
        if n not in con_barra:
            normales = en_quad.get(n, set())
            for nx_, ny_ in normales:
                if abs(nx_) > 0.95:
                    rx = True
                elif abs(ny_) > 0.95:
                    ry = True
                else:
                    rx = ry = True
            rz = True if normales else rz
        elif rigidas.get(n, 0) == 0 and n not in en_quad:
            rx = ry = rz = True
        m.def_support(n, dx, dy, dz, rx, ry, rz)

    # ---- cargas ----
    peso_nudo: dict = {}
    for f, a, b, sec in mod.barras:
        _, g = perfiles[sec]
        m.add_member_dist_load(f, "FZ", -7.85 * g["A"], -7.85 * g["A"], case="DEAD")
        L = float(np.linalg.norm(np.array(mod.nudos[b]) - np.array(mod.nudos[a])))
        for n in (a, b):
            peso_nudo[n] = peso_nudo.get(n, 0.0) + 7.85 * g["A"] * L / 2
    for nombre, ns, sec in mod.areas:
        a = mod.secciones_area[sec]
        pts = np.array([mod.nudos[n] for n in ns])
        nrm = np.array([*mod.normal_area[nombre], 0.0])
        u = np.cross(np.array([0, 0, 1.0]), nrm)
        tt, zz = pts @ u, pts[:, 2]
        area = 0.5 * abs(sum(tt[k] * zz[(k + 1) % len(ns)] - tt[(k + 1) % len(ns)] * zz[k] for k in range(len(ns))))
        w = mod.materiales[a["material"]]["gamma"] * a["espesor"] * area / len(ns)
        for n in ns:
            m.add_node_load(n, "FZ", -w, case="DEAD")
            peso_nudo[n] = peso_nudo.get(n, 0.0) + w
    for f, pat, (fx, fy_, fz) in cargas.barras:
        _, a, b, _ = next(x for x in mod.barras if x[0] == f)
        for d_, v in (("FX", fx), ("FY", fy_), ("FZ", fz)):
            if abs(v) > 1e-12:
                m.add_member_dist_load(f, d_, v, v, case=pat)
        if pat == "DEAD":
            L = float(np.linalg.norm(np.array(mod.nudos[b]) - np.array(mod.nudos[a])))
            for n in (a, b):
                peso_nudo[n] = peso_nudo.get(n, 0.0) + abs(fz) * L / 2
    for n, pat, (fx, fy_, fz) in cargas.nudos:
        for d_, v in (("FX", fx), ("FY", fy_), ("FZ", fz)):
            if abs(v) > 1e-12:
                m.add_node_load(n, d_, v, case=pat)
    estaticos = [n for n, t in cargas.patrones.items() if t != "QUAKE"]
    for n in estaticos:
        m.add_load_combo(n, {n: 1.0})
    paso(5, f"Análisis estático: {len(estaticos)} casos de carga", 60)
    _calc = Analysis._calc_reactions
    Analysis._calc_reactions = lambda *a_, **k_: None
    Analysis._unpartition = _unpartition_rapido
    try:
        m.analyze_linear(check_statics=False)
    except Exception as e:
        if "singular" in str(e).lower() or "unstable" in str(e).lower():
            raise ModeloInestable(_mecanismo(m, mod)) from e
        raise
    finally:
        Analysis._calc_reactions = _calc
    K = _reacciones(m, estaticos)

    # ---- modal: masa concentrada (DX, DY, DZ) ----
    paso(60, "Análisis modal (masa de las cargas permanentes)", 72)
    D1_idx, D2_idx, D2 = Analysis._partition_D(m)
    K11, _, K21, _ = Analysis._partition(m, K, D1_idx, D2_idx)
    diag = np.zeros(len(m.nodes) * 6)
    for n, w in peso_nudo.items():
        i = m.nodes[n].ID * 6
        diag[i] = diag[i + 1] = diag[i + 2] = w / G_ACC
    M11, _, _, _ = Analysis._partition(m, sps.diags(diag).tocsr(), D1_idx, D2_idx)
    infl = {k: np.zeros(len(m.nodes) * 6) for k in "XYZ"}
    for nd in m.nodes.values():
        for k, o in zip("XYZ", (0, 1, 2)):
            infl[k][nd.ID * 6 + o] = 1.0
    r = {k: infl[k][D1_idx] for k in "XYZ"}
    Mr = {k: M11 @ r[k] for k in "XYZ"}
    masa = {k: float(r[k] @ Mr[k]) for k in "XYZ"}
    nmax = min(K11.shape[0] - 2, MODOS_MAX)
    nmod = min(max(int(pc.modos), 6), nmax)
    Kc, Mc = K11.tocsc(), M11.tocsc()
    while True:
        try:
            vals, vecs = sla.eigsh(Kc, k=nmod, M=Mc, sigma=0.0, which="LM")
        except (sla.ArpackError, sla.ArpackNoConvergence, RuntimeError):
            A = np.linalg.solve(Kc.toarray(), Mc.toarray())
            mu, V = np.linalg.eig(A)
            ok = (np.abs(mu.imag) < 1e-9 * max(np.abs(mu).max(), 1e-30)) & (mu.real > 1e-14 * max(np.abs(mu).max(), 1e-30))
            mu, V = mu.real[ok], V.real[:, ok]
            o_ = np.argsort(-mu)[:nmod]
            vals, vecs = 1.0 / mu[o_], V[:, o_]
            nmod = len(vals)
            nmax = min(nmax, nmod)
        o_ = np.argsort(vals)
        vals, vecs = vals[o_], vecs[:, o_]
        mm = np.einsum("ij,ij->j", vecs, np.asarray(M11 @ vecs))
        gam = {k: (vecs.T @ Mr[k]) / mm for k in "XYZ"}
        mef = {k: gam[k] ** 2 * mm for k in "XYZ"}
        part = {k: (mef[k].sum() / masa[k] if masa[k] else 1.0) for k in "XYZ"}
        if (part["X"] >= MASA_OBJETIVO and part["Y"] >= MASA_OBJETIVO) or nmod >= nmax:
            break
        paso(66, f"Análisis modal: {nmod} modos, masa X {100 * part['X']:.0f} %, Y {100 * part['Y']:.0f} %; agregando modos", 72)
        nmod = min(2 * nmod, nmax)
    omegas = np.sqrt(np.maximum(vals, 1e-12))
    periodos = 2 * math.pi / omegas

    # ---- espectro por dirección, corte basal entre Qmín y Qmáx ----
    paso(72, "Espectro NCh433:2026 y corte basal en X e Y", 74)
    sis = sistema_sismico(pc)
    s_suelo = SUELOS_DS61[pc.suelo][0]
    a0g = A0_ZONA[pc.zona]
    peso = masa["X"] * G_ACC
    q_min = pc.importancia_sismo * s_suelo * a0g * peso / 6
    q_max = pc.importancia_sismo * cmax(sis["R"]) * s_suelo * a0g * peso
    dirs = {}
    for k in "XY":
        i_f = int(np.argmax(mef[k]))
        t_est = float(periodos[i_f])
        esp = espectro_ds61(pc, t_est)
        tn, sa = zip(*esp)
        Sa = lambda T, tn=tn, sa=sa: float(np.interp(T, tn, sa)) * G_ACC               # noqa: E731
        q_modo = [float(gam[k][i] * (vecs[:, i] @ Mr[k]) * Sa(periodos[i])) for i in range(nmod)]
        q0 = float(_cqc([np.array([q]) for q in q_modo], list(omegas))[0])
        factor = 1.0
        if 0 < q0 < q_min:
            factor = q_min / q0
        elif q0 > q_max:
            factor = q_max / q0
        r_est = 1 + t_est / (0.10 * SUELOS_DS61[pc.suelo][1] + t_est / sis["R0"])
        dirs[k] = {"T": t_est, "Sa": Sa, "q0": q0, "factor": factor, "R_estrella": r_est, "espectro": esp, "i_fund": i_f}
    Sv = lambda T: FACTOR_VERTICAL * max(dirs["X"]["Sa"](T), dirs["Y"]["Sa"](T))         # noqa: E731
    coef = {k: np.array([dirs[k]["factor"] * gam[k][i] * dirs[k]["Sa"](periodos[i]) / omegas[i] ** 2 for i in range(nmod)]) for k in "XY"}
    coef["Z"] = np.array([gam["Z"][i] * Sv(periodos[i]) / omegas[i] ** 2 for i in range(nmod)])
    aporta = [i for i in range(nmod) if i < pc.modos or any(masa[k] and mef[k][i] / masa[k] > 1e-4 for k in "XYZ")]
    modos_combo = []
    for i in aporta:
        nombre = f"_MODO{i + 1}"
        m.add_load_combo(nombre, {})
        Analysis._store_displacements(m, vecs[:, i].reshape(-1, 1), D2 * 0, D1_idx, D2_idx, m.load_combos[nombre])
        modos_combo.append(nombre)
    om_a = [float(omegas[i]) for i in aporta]
    cx, cy, cz = coef["X"][aporta], coef["Y"][aporta], coef["Z"][aporta]
    factor_d = {k: max(dirs[k]["factor"], 1.0) / dirs[k]["factor"] for k in "XY"}  # Qmáx no reduce desplazamientos
    # masa vertical que no captan los modos: respuesta estática con Sa(0)
    r_res = r["Z"] - vecs @ gam["Z"]
    f_res = np.asarray(M11 @ r_res).ravel() * Sv(0.0)
    d_res = sla.spsolve(K11.tocsc(), f_res).reshape(-1, 1)
    m.add_load_combo("_RESZ", {})
    Analysis._store_displacements(m, d_res, D2 * 0, D1_idx, D2_idx, m.load_combos["_RESZ"])

    # torsión accidental (6.3.3): × (1 + 0,6·|d|/B) según la distancia en planta al centro de masas, perpendicular al sismo
    xyz = np.array(list(mod.nudos.values()))
    pesos = np.array([peso_nudo.get(n, 0.0) for n in mod.nudos])
    cm = (pesos @ xyz[:, :2] / pesos.sum()) if pesos.sum() > 0 else xyz[:, :2].mean(axis=0)
    Bx, By = float(np.ptp(xyz[:, 0])) or 1.0, float(np.ptp(xyz[:, 1])) or 1.0

    def f_tor(pt, k):
        if k == "X":
            return 1 + 0.6 * abs(pt[1] - cm[1]) / By
        return 1 + 0.6 * abs(pt[0] - cm[0]) / Bx

    def env_e(val, pt):
        """val(combo) → array; pt: punto en planta del elemento. EX, EY (con torsión accidental), EZ y E (100/30)."""
        resp = [np.asarray(val(c), float) for c in modos_combo]
        ex = _cqc([k * v for k, v in zip(cx, resp)], om_a) * f_tor(pt, "X")
        ey = _cqc([k * v for k, v in zip(cy, resp)], om_a) * f_tor(pt, "Y")
        ez = np.sqrt(_cqc([k * v for k, v in zip(cz, resp)], om_a) ** 2 + np.asarray(val("_RESZ"), float) ** 2)
        return ex, ey, ez, np.maximum.reduce([ex + 0.3 * ey + 0.3 * ez, 0.3 * ex + ey + 0.3 * ez, 0.3 * ex + 0.3 * ey + ez])

    # ---- fuerzas en las barras: N, Vy, Vz, T, My (fuerte), Mz (débil) en n_puntos ----
    sin_cargas = set(modos_combo) | {"_RESZ"}
    t_unit = np.linspace(0.0, 1.0, n_puntos)

    def fuerzas(f, combo):
        mb = m.members[f]
        if combo in sin_cargas:
            fl = np.asarray(mb.f(combo)).ravel()
            return (np.full(n_puntos, fl[0]), np.full(n_puntos, fl[1]), np.full(n_puntos, fl[2]),
                    -fl[4] + (fl[10] + fl[4]) * t_unit, fl[5] + (-fl[11] - fl[5]) * t_unit)
        L = mb.L()
        xs = np.linspace(0, L, n_puntos)
        return (np.array([mb.axial(x, combo) for x in xs]), np.array([mb.shear("Fy", x, combo) for x in xs]),
                np.array([mb.shear("Fz", x, combo) for x in xs]), np.array([mb.moment("My", x, combo) for x in xs]),
                np.array([mb.moment("Mz", x, combo) for x in xs]))
    res_barra = {}
    for k_b, (f, a, b, _) in enumerate(mod.barras):
        if k_b % 10 == 0:
            paso(74 + 16 * k_b / max(len(mod.barras), 1), f"Esfuerzos en las barras ({k_b + 1} de {len(mod.barras)})", 90)
        est = {c: fuerzas(f, c) for c in estaticos}
        cache = {}

        def comp(c, k, f=f):
            if c not in cache:
                cache[c] = fuerzas(f, c)
            return cache[c][k]
        pt = (np.array(mod.nudos[a]) + np.array(mod.nudos[b])) / 2
        E = tuple(env_e(lambda c, k=k: comp(c, k), pt)[3] for k in range(5))
        res_barra[f] = (est, E)

    # ---- diseño de acero AISC 360-16 (flexión biaxial) por elemento de Revit ----
    paso(90, "Diseño de acero AISC 360-16", 94)
    por_elemento = {}
    for f, a, b, sec in mod.barras:
        por_elemento.setdefault((mod.origen.get(f), sec), []).append(f)
    acero = []
    for (orig, sec), fs in por_elemento.items():
        p, g = perfiles[sec]
        L = sum(m.members[f].L() for f in fs)
        tramos = [m.members[f].L() for f in fs]
        L_plano = max(tramos)                         # eje fuerte: entre los nudos a los que llegan otros elementos
        lf = mod.l_fuera.get(str(orig))
        if lf == "plano":
            L_fuera = L_plano
        else:
            try:
                L_fuera = min(float(lf), L) if lf not in (None, "", "completo") else L
            except (TypeError, ValueError):
                L_fuera = L
        cap = _capacidades3d(p, g, L_plano, L_fuera, L_fuera, fy)
        rol_e = mod.roles.get(fs[0], "viga")
        peor = None
        for cn, fac in cargas.combos.items():
            fe = fac.get("E", 0.0)
            for f in fs:
                est, E = res_barra[f]
                tot = [sum(fac.get(c, 0) * est[c][k] for c in estaticos if c in fac) for k in range(5)]
                for s in ((1,), (-1,)) if fe else ((0,),):
                    N = tot[0] + s[0] * fe * E[0]
                    Vy = np.abs(tot[1]) + abs(fe) * E[1]
                    Vz = np.abs(tot[2]) + abs(fe) * E[2]
                    My = np.abs(tot[3]) + abs(fe) * E[3]
                    Mz = np.abs(tot[4]) + abs(fe) * E[4]
                    for k in range(n_puntos):
                        rz = _razon_biaxial(float(N[k]), float(My[k]), float(Mz[k]), float(max(Vy[k], Vz[k])), cap)
                        val = max(rz["interaccion"], rz["corte"])
                        if peor is None or val > peor["razon"]:
                            peor = {"razon": val, "interaccion": rz["interaccion"], "corte": rz["corte"], "combo": cn,
                                    "Pu": float(N[k]), "Mux": float(My[k]), "Muy": float(Mz[k]),
                                    "Vu": float(max(Vy[k], Vz[k])), "ecuacion": rz["ecuacion"]}
        notas = list(cap["notas"])
        pu_rel = (peor or {}).get("Pu", 0.0) / cap["phiPn"] if cap["phiPn"] > 0 else 0.0
        if rol_e not in ("pilar", "diagonal") and pu_rel < 0.2:
            notas = [n for n in notas if not (n.startswith("esbeltez") or "en compresión" in n)]
        acero.append({"origen": orig, "seccion": sec, "rol": rol_e, "barras": fs, "L": L, "L_plano": L_plano, "L_fuera": L_fuera,
                      **(peor or {"razon": 0.0}), "phiPn": cap["phiPn"], "phiMnx": cap["phiMn"], "phiMny": cap["phiMny"],
                      "phiVn": cap["phiVn"], "KL_r": cap["KL/r"], "notas": notas})
    acero.sort(key=lambda r_: -r_["razon"])

    # ---- muros de madera: corte en su plano en la fila de la base de cada segmento ----
    paso(94, "Verificación de muros de madera", 97)
    asd = {"D+W": {"DEAD": 1.0, "W": 1.0}, "D+E": {"DEAD": 1.0, "E": 1.0}, "D+0,75W+0,75Lr": {"DEAD": 1.0, "W": 0.75, "LR": 0.75},
           "D+0,75E": {"DEAD": 1.0, "E": 0.75}, "0,6D+W": {"DEAD": 0.6, "W": 1.0}, "0,6D+E": {"DEAD": 0.6, "E": 1.0}}
    vientos = [n for n, t in cargas.patrones.items() if t == "WIND"]
    madera = []
    for seg in mod.resumen_muros:
        (ax_, ay_), (ux, uy) = seg["a"], seg["u"]
        x0, x1, H = seg["x0"], seg["x1"], seg["H"]
        cuads = [(nombre, ns) for nombre, ns, sec in mod.areas if sec == seg["seccion"]]
        if not cuads:
            continue

        def tz(n):
            x, y, z = mod.nudos[n]
            return (x - ax_) * ux + (y - ay_) * uy, z
        z_min = min(tz(n)[1] for _, ns in cuads for n in ns)
        fila = [(nombre, ns) for nombre, ns in cuads if min(tz(n)[1] for n in ns) <= z_min + 1e-6]
        t_ = mod.secciones_area[seg["seccion"]]["espesor"]

        def V_fila(combo, comp=2):
            v = 0.0
            for nombre, ns in fila:
                ts = [tz(n)[0] for n in ns]
                vals_ = []
                for q in sub[nombre]:
                    s_ = np.asarray(m.quads[q].membrane(0, 0, True, combo)).ravel()
                    vals_.append(s_[comp])
                v += float(np.mean(vals_)) * t_ * (max(ts) - min(ts))
            return v
        Vest = {c: V_fila(c) for c in ["DEAD", "LR"] + vientos}
        Nest = {c: -V_fila(c, 1) for c in ("DEAD", "LR")}
        pt = (ax_ + ux * (x0 + x1) / 2, ay_ + uy * (x0 + x1) / 2)
        VE = float(env_e(lambda c: np.array([V_fila(c)]), pt)[3][0])
        NEz = float(env_e(lambda c: np.array([V_fila(c, 1)]), pt)[2][0])
        pm = seg.get("parametros", {})
        vn = VN_OSB_9_5_8D.get(int(pm.get("espaciamiento_clavos", 100)), 0.48) * int(pm.get("tableros", 1))
        L = seg["L"]
        peor = None
        peor_t = (0.0, "")
        b_ext = float(pm.get("b_ext", 0.10))
        Lp = max(L - 1.5 * b_ext - 0.1, 0.3)
        for cn, fac in asd.items():
            for w in (vientos if "W" in fac else [None]):
                V = abs(sum(fac.get(c, 0) * Vest[c] for c in ("DEAD", "LR")) + (fac.get("W", 0) * Vest[w] if w else 0.0)) \
                    + fac.get("E", 0) * VE
                cap_ = vn * (1.4 if w else 1.0)
                fu = V / L / cap_
                if peor is None or fu > peor["FU"]:
                    peor = {"FU": fu, "combo": cn + (f" ({w})" if w else ""), "v": V / L, "vn": cap_}
                Ng = max(sum(fac.get(c, 0) * Nest[c] for c in ("DEAD", "LR")) - fac.get("E", 0) * NEz, 0.0)
                T = V * H / Lp - Ng * L / (2 * Lp)
                if T > peor_t[0]:
                    peor_t = (T, cn + (f" ({w})" if w else ""))
        madera.append({"muro": seg["muro"], "x0": x0, "x1": x1, "L": L, "H": H, **peor, "T_anclaje": peor_t[0],
                       "combo_anclaje": peor_t[1]})
    madera.sort(key=lambda r_: -r_["FU"])

    # ---- deformaciones: deriva sísmica y lateral por viento de los pilares; flecha vertical de las vigas ----
    lim = {"viga_total": 240.0, "lateral_viento": 200.0, "deriva_sismo": sis["deriva"], **(limites or {})}
    deform = []
    pil = {}
    for f, a, b, _ in mod.barras:
        if mod.roles.get(f) == "pilar":
            pil.setdefault(mod.origen.get(f), set()).update((a, b))
    for orig, ns in pil.items():
        ns = sorted(ns, key=lambda n: mod.nudos[n][2])
        nb, nt = ns[0], ns[-1]
        Hh = mod.nudos[nt][2] - mod.nudos[nb][2]
        if Hh <= 0.1:
            continue
        for k, attr in (("X", "DX"), ("Y", "DY")):
            dd = lambda c, a=attr: np.array([getattr(m.nodes[nt], a)[c] - getattr(m.nodes[nb], a)[c]])   # noqa: E731
            coefk = cx if k == "X" else cy
            resp = [dd(c) for c in modos_combo]
            de = float(_cqc([kk * v for kk, v in zip(coefk, resp)], om_a)[0]) * f_tor(mod.nudos[nt][:2], k) * factor_d[k]
            deform.append({"elemento": orig, "tipo": f"Pilar (deriva sísmica {k})", "combo": f"E{k} (espectral, CQC)",
                           "delta_mm": de * 1000, "limite_mm": lim["deriva_sismo"] * Hh * 1000,
                           "criterio": f"{lim['deriva_sismo']}·H", "L": Hh, "razon": de / (lim["deriva_sismo"] * Hh)})
            dw = max((abs(float(dd(w)[0] + dd("DEAD")[0])) for w in vientos), default=0.0)
            deform.append({"elemento": orig, "tipo": f"Pilar (lateral por viento {k})", "combo": "D+W",
                           "delta_mm": dw * 1000, "limite_mm": Hh / lim["lateral_viento"] * 1000,
                           "criterio": f"H/{lim['lateral_viento']:g}", "L": Hh, "razon": dw / (Hh / lim["lateral_viento"])})
    vg = {}
    for f, a, b, _ in mod.barras:
        if mod.roles.get(f) in ("viga", "techo"):
            vg.setdefault(mod.origen.get(f), []).append((a, b))
    for orig, segs in vg.items():
        ns = []
        for a, b in segs:
            ns += [a, b]
        ns = list(dict.fromkeys(ns))
        pts = np.array([mod.nudos[n] for n in ns])
        d_ = pts - pts[0]
        eje = d_[np.argmax(np.linalg.norm(d_, axis=1))]
        Lv = float(np.linalg.norm(eje)) or 1.0
        ts = d_ @ (eje / Lv)
        o_ = np.argsort(ts)
        ns = [ns[i] for i in o_]
        ts = ts[o_]
        dz = np.array([m.nodes[n].DZ["DEAD"] + m.nodes[n].DZ["LR"] for n in ns])
        flecha = float(np.max(np.abs(dz - (dz[0] + (dz[-1] - dz[0]) * (ts - ts[0]) / max(ts[-1] - ts[0], 1e-9)))))
        deform.append({"elemento": orig, "tipo": "Viga (vertical)", "combo": "D+Lr", "delta_mm": flecha * 1000,
                       "limite_mm": Lv / lim["viga_total"] * 1000, "criterio": f"L/{lim['viga_total']:g}", "L": Lv,
                       "razon": flecha / (Lv / lim["viga_total"])})
    deform.sort(key=lambda d: -d["razon"])

    # ---- reacciones basales por estado ----
    apoyos = list(mod.apoyos)
    casos = []
    for c in estaticos:
        rx = sum(m.nodes[n].RxnFX[c] for n in apoyos)
        ry = sum(m.nodes[n].RxnFY[c] for n in apoyos)
        rz = sum(m.nodes[n].RxnFZ[c] for n in apoyos)
        casos.append({"caso": c, "tipo": cargas.patrones.get(c), "Rx": rx, "Ry": ry, "Rz": rz})
    for k in "XY":
        casos.append({"caso": f"E{k}", "tipo": "QUAKE", "corte_basal": dirs[k]["q0"] * dirs[k]["factor"],
                      "corte_espectral": dirs[k]["q0"], "factor": dirs[k]["factor"], "T": dirs[k]["T"]})

    # ---- visor 3D ----
    paso(97, "Preparando el visor 3D", 100)
    razon_barra = {}
    for a_ in acero:
        for f in a_["barras"]:
            razon_barra[f] = a_["razon"]
    ver_modos = []
    pX = mef["X"] / masa["X"] if masa["X"] else mef["X"] * 0
    pY = mef["Y"] / masa["Y"] if masa["Y"] else mef["Y"] * 0
    for i in aporta[:12]:
        nombre = f"_MODO{i + 1}"
        forma = {n: [round(m.nodes[n].DX[nombre], 5), round(m.nodes[n].DY[nombre], 5), round(m.nodes[n].DZ[nombre], 5)] for n in mod.nudos}
        ver_modos.append({"n": i + 1, "T": float(periodos[i]), "masa_x": float(pX[i]), "masa_y": float(pY[i]), "forma": forma})
    visor = {"nudos": {n: list(p) for n, p in mod.nudos.items()},
             "barras": [[f, a, b, mod.origen.get(f), mod.roles.get(f), sec, razon_barra.get(f)] for f, a, b, sec in mod.barras],
             "quads": [[nombre, ns, mod.origen.get(nombre)] for nombre, ns, _ in mod.areas],
             "apoyos": mod.apoyos, "modos": ver_modos,
             "deformada_D": {n: [m.nodes[n].DX["DEAD"], m.nodes[n].DY["DEAD"], m.nodes[n].DZ["DEAD"]] for n in mod.nudos}}
    madera_fu = {m_["muro"]: max(m_["FU"], 0) for m_ in madera}
    visor["fu_muros"] = madera_fu
    return {
        "version": VERSION_3D, "tipo": "3d", "periodos": [float(x) for x in periodos[:60]],
        "masa_participante": {k: float(part[k]) for k in "XYZ"}, "n_modos": int(nmod),
        "direcciones": {k: {kk: (v if kk != "Sa" else None) for kk, v in d.items() if kk != "Sa"} for k, d in dirs.items()},
        "q_min": q_min, "q_max": q_max, "peso_sismico": peso, "sistema": sis, "centro_masas": [float(cm[0]), float(cm[1])],
        "acero": acero, "madera": madera, "deformaciones": deform, "casos": casos, "combos_lrfd": cargas.combos,
        "patrones": cargas.patrones, "resumen_cargas": cargas.resumen, "avisos": mod.avisos, "sin_apoyo": mod.sin_apoyo,
        "visor": visor, "conteo": {"nudos": len(mod.nudos), "barras": len(mod.barras), "shells": len(mod.areas)},
    }
