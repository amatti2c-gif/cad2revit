"""Análisis y diseño del modelo 2D de un eje con PyNite (sin SAP2000), en tonf y m.

- Modelo plano: X horizontal, Y vertical (gravedad −Y), Z fuera del plano (restringido). Barras de acero y muros
  como cuadriláteros MITC4 (los triángulos bajo el techo van como cuadriláteros con un nudo repetido), con el E y G
  calibrados de cada segmento de muro.
- Casos estáticos: DEAD (peso propio + peso del techo), LR y los casos de viento.
- Sismo E: análisis modal con la masa de DEAD y espectro NCh433 + DS61 (R* con el T* del modelo), combinación
  modal CQC; las fuerzas de E son envolventes (±).
- Combinaciones NCh3171 por resistencia (acero, AISC 360-16) y por tensiones admisibles (muros de madera).
"""

from __future__ import annotations

import math

import numpy as np

from .diseno_acero import FY_A270ES, Perfil, capacidades, razon
from .eje_cargas import A0_ZONA, SUELOS_DS61, espectro_ds61

G_ACC = 9.80665
# resistencia admisible al corte de muros (tonf/m), 1 tablero OSB 9,5 mm Sheathing y clavo 8d, según el clavado
# perimetral (Manual de diseño de estructuras en madera, Tablas 45 y 46; SDPWS 4.3A, sismo). Viento: × 1,4 (SDPWS)
VN_OSB_9_5_8D = {150: 0.33, 100: 0.48, 50: 0.79}


def _cmax(r: float) -> float:
    """NCh433 Tabla 6.4: Cmax / (S·A0/g) según R."""
    return float(np.interp(r, [2, 3, 4, 5.5, 6, 7], [0.90, 0.60, 0.55, 0.40, 0.35, 0.35]))


def _cqc(valores: list[np.ndarray], omegas: list[float], amort: float = 0.05) -> np.ndarray:
    n = len(valores)
    tot = np.zeros_like(valores[0])
    for i in range(n):
        for j in range(n):
            r = omegas[j] / omegas[i]
            rho = 8 * amort ** 2 * (1 + r) * r ** 1.5 / ((1 - r ** 2) ** 2 + 4 * amort ** 2 * r * (1 + r) ** 2)
            tot = tot + rho * valores[i] * valores[j]
    return np.sqrt(np.abs(tot))


def _txy_global(q, combo) -> float:
    """τxy en ejes globales en el centro de un cuadrilátero (PyNite 3.2 falla al pedirlo con local=False)."""
    sx, sy, txy = (float(np.ravel(v)[0]) for v in q.membrane(0.0, 0.0, True, combo))
    T = q.T()[:3, :3]
    S = T.T @ np.array([[sx, txy, 0.0], [txy, sy, 0.0], [0.0, 0.0, 0.0]]) @ T
    return float(S[0, 1])


def analizar(mod, cargas, pc, *, fy: float = FY_A270ES, lb_fuera: float | None = None, n_puntos: int = 9,
             avance=None, limites: dict | None = None) -> dict:
    """mod: eje_revit.ModeloEje; cargas: eje_cargas.CargasEje; pc: ParametrosCargas.
    avance(pct, etapa, hasta): opcional, se llama a medida que avanza el cálculo (pct de 0 a 100; hasta: el pct
    de la etapa siguiente, para que la barra avance sola durante una etapa larga, como el análisis estático)."""
    from Pynite import FEModel3D
    from Pynite import Analysis

    def paso(pct, etapa, hasta=None):
        if avance:
            avance(round(pct), etapa, round(hasta if hasta is not None else pct))
    paso(2, "Armando el modelo en PyNite")

    m = FEModel3D()
    for n, (x, z) in mod.nudos.items():
        m.add_node(n, x, z, 0.0)
    # materiales: acero y uno por sección de muro (E y G calibrados)
    for nombre, d in mod.materiales.items():
        E = d["E"]
        m.add_material(nombre, E, E / (2 * (1 + d["nu"])), d["nu"], 0.0)
    mat_area = {}
    for sec, a in mod.secciones_area.items():
        base = mod.materiales[a["material"]]
        E0, G0 = base["E"], base["E"] / (2 * (1 + base["nu"]))
        nombre = f"MAT_{sec}"
        # PyNite (Quad3D.Cm) toma G = E/(2(1+ν)) y no el G del material: con ν = 0 el material lleva E = 2·G_calibrado
        # y el E calibrado entra como factor ortótropo kx = ky = E_cal / (2·G_cal)
        E_cal, G_cal = E0 * a["f11"], G0 * a["f12"]
        m.add_material(nombre, 2 * G_cal, G_cal, 0.0, 0.0)
        mat_area[sec] = (nombre, E_cal / (2 * G_cal))
    perfiles = {}
    for sec, s in mod.secciones.items():
        p = Perfil(s["forma"], s["d"], s["bf"], s.get("tw", 0.0), s.get("tf", 0.0), s.get("t", 0.0))
        g = p.props()
        perfiles[sec] = (p, g)
        m.add_section(sec, g["A"], g["Iy"], g["Ix"], g["J"])          # PyNite: Iz = eje fuerte (flexión en el plano)
    for f, a, b, sec in mod.barras:
        m.add_member(f, a, b, "ACERO", sec)
    # PyNite no tiene triángulos: cada triángulo se divide en 3 cuadriláteros (centroide y puntos medios de los lados)
    sub = {}                                                          # área → [cuadriláteros de PyNite]
    extra = {}

    def nudo_xy(x, z):
        k = (round(x, 6), round(z, 6))
        if k not in extra:
            extra[k] = f"_P{len(extra) + 1}"
            m.add_node(extra[k], x, z, 0.0)
        return extra[k]
    for nombre, ns, sec in mod.areas:
        t_ = mod.secciones_area[sec]["espesor"]
        if len(ns) == 4:
            m.add_quad(nombre, *ns, t_, mat_area[sec][0], mat_area[sec][1], mat_area[sec][1])
            sub[nombre] = [nombre]
            continue
        P = [mod.nudos[n] for n in ns]
        g = nudo_xy(sum(p[0] for p in P) / 3, sum(p[1] for p in P) / 3)
        med = [nudo_xy((P[k][0] + P[(k + 1) % 3][0]) / 2, (P[k][1] + P[(k + 1) % 3][1]) / 2) for k in range(3)]
        sub[nombre] = []
        for k in range(3):
            q = f"{nombre}_{k + 1}"
            m.add_quad(q, ns[k], med[k], g, med[k - 1], t_, mat_area[sec][0], mat_area[sec][1], mat_area[sec][1])
            sub[nombre].append(q)
    # apoyos y restricciones del plano (Z, RX, RY); los nudos solo de muros no tienen giro en el plano (RZ)
    con_barra = {x for _, a, b, _ in mod.barras for x in (a, b)}
    for n in mod.nudos:
        t = mod.apoyos.get(n)
        m.def_support(n, t is not None, t is not None, True, True, True, (t == "empotrado") or (n not in con_barra))
    for n in extra.values():
        m.def_support(n, False, False, True, True, True, True)

    # ---- cargas ----
    for f, a, b, sec in mod.barras:                                   # peso propio del acero
        _, g = perfiles[sec]
        m.add_member_dist_load(f, "FY", -7.85 * g["A"], -7.85 * g["A"], case="DEAD")
    for nombre, ns, sec in mod.areas:                                 # peso de los muros a sus nudos
        a = mod.secciones_area[sec]
        pts = [mod.nudos[n] for n in ns]
        area = abs(sum(pts[k][0] * pts[(k + 1) % len(pts)][1] - pts[(k + 1) % len(pts)][0] * pts[k][1]
                       for k in range(len(pts)))) / 2
        w = mod.materiales[a["material"]]["gamma"] * a["espesor"] * area / len(ns)
        for n in ns:
            m.add_node_load(n, "FY", -w, case="DEAD")
    geo = {f: (mod.nudos[a], mod.nudos[b]) for f, a, b, _ in mod.barras}
    for f, pat, d, v in cargas.distribuidas:
        (x0, z0), (x1, z1) = geo[f]
        L = math.hypot(x1 - x0, z1 - z0)
        if d == "X":
            m.add_member_dist_load(f, "FX", v, v, case=pat)
        elif d == "Z":
            m.add_member_dist_load(f, "FY", v, v, case=pat)
        elif d == "GRAV":
            m.add_member_dist_load(f, "FY", -v, -v, case=pat)
        else:                                                         # por proyección horizontal
            w = -v * abs(x1 - x0) / L
            m.add_member_dist_load(f, "FY", w, w, case=pat)
    estaticos = [n for n, t in cargas.patrones.items() if t != "QUAKE"]
    for n in estaticos:
        m.add_load_combo(n, {n: 1.0})

    # ---- modal (masa = DEAD) y espectro DS61 con CQC ----
    paso(5, f"Análisis estático: {len(estaticos)} casos de carga", 75)
    m.analyze_linear(check_statics=False)
    paso(75, "Análisis modal (masa del peso propio)", 80)
    D1_idx, D2_idx, D2 = Analysis._partition_D(m)
    K11, _, _, _ = Analysis._partition(m, m.Ke("DEAD", False, True, sparse=True).tocsr(), D1_idx, D2_idx)
    M11, _, _, _ = Analysis._partition(m, m.M("DEAD", "Y", G_ACC, False, sparse=True).tocsr(), D1_idx, D2_idx)
    import scipy.sparse.linalg as sla
    nmod = min(pc.modos, K11.shape[0] - 2)
    vals, vecs = sla.eigsh(K11.tocsc(), k=nmod, M=M11.tocsc(), sigma=0.0, which="LM")
    orden = np.argsort(vals)
    vals, vecs = vals[orden], vecs[:, orden]
    omegas = np.sqrt(np.maximum(vals, 1e-12))
    periodos = 2 * math.pi / omegas
    # vector de influencia en X: los grados de libertad DX de los nudos libres
    gdl = []
    for n in m.nodes.values():
        base = n.ID * 6
        gdl += [base + k for k in range(6)]
    infl_full = np.zeros(len(m.nodes) * 6)
    for n in m.nodes.values():
        infl_full[n.ID * 6 + 0] = 1.0
    r = infl_full[D1_idx]
    Mr = M11 @ r
    masa_tot = float(r @ Mr)
    gam = np.array([vecs[:, i] @ Mr / (vecs[:, i] @ (M11 @ vecs[:, i])) for i in range(nmod)])
    m_ef = np.array([gam[i] ** 2 * (vecs[:, i] @ (M11 @ vecs[:, i])) for i in range(nmod)])
    i_fund = int(np.argmax(m_ef))
    t_est = float(periodos[i_fund])
    espectro = espectro_ds61(pc, t_est)
    tn, sa = zip(*espectro)

    def Sa(T):
        return float(np.interp(T, tn, sa)) * G_ACC

    # corte basal por modo (Γ·Lᵢ·Sa) combinado con CQC y llevado entre Qmín y Qmáx (NCh433 6.3.7)
    q_modo = [float(gam[i] * (vecs[:, i] @ Mr) * Sa(periodos[i])) for i in range(nmod)]
    q0 = float(_cqc([np.array([q]) for q in q_modo], list(omegas))[0])
    peso = masa_tot * G_ACC
    s_suelo = SUELOS_DS61[pc.suelo][0]
    a0g = A0_ZONA[pc.zona]
    c_max = _cmax(pc.r0) * s_suelo * a0g
    q_min = pc.importancia_sismo * s_suelo * a0g * peso / 6
    q_max = pc.importancia_sismo * c_max * peso
    factor = 1.0
    if q0 > 0 and q0 < q_min:
        factor = q_min / q0
    elif q0 > q_max:
        factor = q_max / q0
    paso(80, f"Espectro NCh433 + DS61 (T* = {t_est:.3f} s) y corte basal")
    modos_combo = []
    for i in range(nmod):
        nombre = f"_MODO{i + 1}"
        m.add_load_combo(nombre, {})
        d1 = (factor * gam[i] * Sa(periodos[i]) / omegas[i] ** 2 * vecs[:, i]).reshape(-1, 1)
        Analysis._store_displacements(m, d1, D2 * 0, D1_idx, D2_idx, m.load_combos[nombre])
        modos_combo.append(nombre)

    # ---- fuerzas en las barras: casos estáticos y envolvente E (CQC), en n_puntos por barra ----
    def fuerzas(f, combo):
        mb = m.members[f]
        L = mb.L()
        xs = np.linspace(0, L, n_puntos)
        return (np.array([mb.axial(x, combo) for x in xs]), np.array([mb.shear("Fy", x, combo) for x in xs]),
                np.array([mb.moment("Mz", x, combo) for x in xs]))

    res_barra = {}
    for k_b, (f, *_) in enumerate(mod.barras):
        paso(82 + 12 * k_b / max(len(mod.barras), 1), f"Esfuerzos en las barras ({k_b + 1} de {len(mod.barras)})")
        est = {c: fuerzas(f, c) for c in estaticos}
        modal = [fuerzas(f, c) for c in modos_combo]
        E = tuple(_cqc([mm[k] for mm in modal], list(omegas)) for k in range(3))
        res_barra[f] = (est, E)

    # ---- diseño de acero AISC 360-16 por elemento de Revit (largo total = largo no arriostrado) ----
    por_elemento = {}
    for f, a, b, sec in mod.barras:
        por_elemento.setdefault((mod.origen.get(f), sec), []).append(f)
    combos_lrfd = cargas.combos
    acero = []
    for k_e, ((orig, sec), fs) in enumerate(por_elemento.items()):
        paso(94 + 4 * k_e / max(len(por_elemento), 1), f"Diseño de acero AISC 360-16 ({k_e + 1} de {len(por_elemento)})")
        p, g = perfiles[sec]
        L = sum(m.members[f].L() for f in fs)
        cap = capacidades(p, L, lb_fuera or L, lb_fuera or L, Fy=fy)
        peor = None
        for cn, fac in combos_lrfd.items():
            for f in fs:
                est, E = res_barra[f]
                N = sum(fac.get(c, 0) * est[c][0] for c in estaticos if c in fac)
                V = sum(fac.get(c, 0) * est[c][1] for c in estaticos if c in fac)
                M = sum(fac.get(c, 0) * est[c][2] for c in estaticos if c in fac)
                fe = fac.get("E", 0.0)
                for s in ((1, 1), (1, -1), (-1, 1), (-1, -1)) if fe else ((0, 0),):
                    Nk = N + s[0] * fe * E[0]
                    Mk = np.abs(M) + abs(fe) * E[2] if fe else np.abs(M)
                    Vk = np.abs(V) + abs(fe) * E[1] if fe else np.abs(V)
                    Pu = -Nk                                               # PyNite: axial + = tracción
                    for k in range(n_puntos):
                        rz = razon(float(Pu[k]), float(Mk[k]), float(Vk[k]), cap)
                        val = max(rz["interaccion"], rz["corte"])
                        if peor is None or val > peor["razon"]:
                            peor = {"razon": val, "interaccion": rz["interaccion"], "corte": rz["corte"], "combo": cn,
                                    "Pu": float(Pu[k]), "Mu": float(Mk[k]), "Vu": float(Vk[k]), "ecuacion": rz["ecuacion"]}
        acero.append({"origen": orig, "seccion": sec, "barras": fs, "L": L, **(peor or {"razon": 0.0}),
                      "phiPn": cap["phiPn"], "phiMn": cap["phiMn"], "phiVn": cap["phiVn"], "KL_r": cap["KL/r"],
                      "notas": cap["notas"]})
    acero.sort(key=lambda r: -r["razon"])

    # ---- muros de madera: corte por segmento y tracción en el anclaje (tensiones admisibles, NCh3171 9.2) ----
    asd = {"D+W": {"DEAD": 1.0, "W": 1.0}, "D+E": {"DEAD": 1.0, "E": 1.0}, "D+0,75W+0,75Lr": {"DEAD": 1.0, "W": 0.75, "LR": 0.75},
           "D+0,75E": {"DEAD": 1.0, "E": 0.75}, "0,6D+W": {"DEAD": 0.6, "W": 1.0}, "0,6D+E": {"DEAD": 0.6, "E": 1.0}}
    vientos = [n for n, t in cargas.patrones.items() if t == "WIND"]
    madera = []
    paso(98, "Verificación de muros de madera")
    for seg in mod.resumen_muros:
        x0, x1, H = seg["x0"], seg["x1"], seg["H"]
        cuads = [(nombre, ns) for nombre, ns, sec in mod.areas if sec == seg["seccion"]]
        if not cuads:
            continue
        # corte a media altura: suma de τxy·t·ancho de los cuadriláteros de esa fila
        zc = lambda ns: sum(mod.nudos[n][1] for n in ns) / len(ns)           # noqa: E731
        z_min = min(zc(ns) for _, ns in cuads)
        z_obj = z_min + H / 2
        fila_z = min({round(zc(ns), 3) for _, ns in cuads}, key=lambda z: abs(z - z_obj))
        fila = [(nombre, ns) for nombre, ns in cuads if abs(round(zc(ns), 3) - fila_z) < 1e-6]
        t = mod.secciones_area[seg["seccion"]]["espesor"]

        def V_fila(combo):
            v = 0.0
            for nombre, ns in fila:
                xs_ = [mod.nudos[n][0] for n in ns]
                txy = sum(_txy_global(m.quads[q], combo) for q in sub[nombre]) / len(sub[nombre])
                v += txy * t * (max(xs_) - min(xs_))
            return v
        Vest = {c: V_fila(c) for c in ["DEAD", "LR"] + vientos}
        VE = float(_cqc([np.array([V_fila(c)]) for c in modos_combo], list(omegas))[0])
        pm = seg.get("parametros", {})
        esp = int(pm.get("espaciamiento_clavos", 100))
        vn = VN_OSB_9_5_8D.get(esp, 0.48) * int(pm.get("tableros", 1))
        L = seg["L"]
        peor = None
        for cn, fac in asd.items():
            opciones = vientos if "W" in fac else [None]
            for w in opciones:
                V = sum(fac.get(c, 0) * Vest[c] for c in ("DEAD", "LR")) + (fac.get("W", 0) * Vest[w] if w else 0.0)
                V = abs(V) + fac.get("E", 0) * VE
                cap = vn * (1.4 if w else 1.0)
                fu = V / L / cap
                if peor is None or fu > peor["FU"]:
                    peor = {"FU": fu, "combo": cn + (f" ({w})" if w else ""), "v": V / L, "vn": cap}
        T = (peor["v"] * L) * H / max(L - 0.1, 0.5) if peor else 0.0
        madera.append({"muro": seg["muro"], "x0": x0, "x1": x1, "L": L, "H": H, **peor, "T_anclaje": T})
    madera.sort(key=lambda r: -r["FU"])

    deformaciones = _deformaciones(m, mod, cargas, modos_combo, list(omegas), sub, {**LIMITES_DEF, **(limites or {})})

    return {
        "T": t_est, "periodos": [float(x) for x in periodos], "masa_participante_x": float(sum(m_ef) / masa_tot) if masa_tot else 0.0,
        "corte_basal_espectral": q0, "q_min": q_min, "q_max": q_max, "factor_corte": factor,
        "corte_basal": q0 * factor, "peso_sismico": peso, "espectro": espectro,
        "acero": acero, "madera": madera, "deformaciones": deformaciones,
        "limites_deformacion": {**LIMITES_DEF, **(limites or {})},
    }


# límites de deformación por defecto (servicio, cargas sin mayorar):
# - vigas y costaneras de techo, flecha relativa a la cuerda: L/240 con cargas totales (D+Lr, D+W, D+0,75Lr+0,75W) y
#   L/360 con la carga variable sola (Lr o W)  [criterio usual, AISC Design Guide 3 / IBC Tabla 1604.3]
# - pilares y muros, desplazamiento lateral relativo: H/200 con viento (D+W) y 0,002·H con sismo (NCh433 5.9.2, con
#   los desplazamientos del análisis espectral reducido)
LIMITES_DEF = {"viga_total": 240, "viga_variable": 360, "lateral_viento": 200, "deriva_sismo": 0.002}


def _deformaciones(m, mod, cargas, modos, omegas, sub, lim) -> list[dict]:
    vientos = [n for n, t in cargas.patrones.items() if t == "WIND"]
    D = lambda n, gdl, caso: getattr(m.nodes[n], gdl)[caso]          # noqa: E731

    def comb(valor, fac):
        return sum(f * valor(c) for c, f in fac.items())
    out = []

    # ---- vigas y techo: flecha perpendicular a la cuerda del elemento de Revit ----
    grupos = {}
    for f, a, b, sec in mod.barras:
        if mod.roles.get(f) in ("viga", "techo"):
            grupos.setdefault(mod.origen.get(f) or f, []).append((a, b))
    totales = [("D+Lr", {"DEAD": 1, "LR": 1})] + [(f"D+{w}", {"DEAD": 1, w: 1}) for w in vientos] +               [(f"D+0,75Lr+0,75{w}", {"DEAD": 1, "LR": 0.75, w: 0.75}) for w in vientos]
    variables = [("Lr", {"LR": 1})] + [(w, {w: 1}) for w in vientos]
    for orig, pares in grupos.items():
        ns = {n for p in pares for n in p}
        (xa, za), (xb, zb) = min((mod.nudos[n] for n in ns)), max((mod.nudos[n] for n in ns))
        L = math.hypot(xb - xa, zb - za)
        if L < 0.5:
            continue
        ux, uz = (xb - xa) / L, (zb - za) / L
        orden = sorted(ns, key=lambda n: (mod.nudos[n][0] - xa) * ux + (mod.nudos[n][1] - za) * uz)
        ini, fin = orden[0], orden[-1]

        def flecha(fac):
            perp = {n: comb(lambda c, n=n: -D(n, "DX", c) * uz + D(n, "DY", c) * ux, fac) for n in orden}
            peor = 0.0
            for n in orden:
                s_ = ((mod.nudos[n][0] - xa) * ux + (mod.nudos[n][1] - za) * uz) / L
                peor = max(peor, abs(perp[n] - (perp[ini] * (1 - s_) + perp[fin] * s_)))
            return peor
        for grupo, lista, div in (("total", totales, lim["viga_total"]), ("variable", variables, lim["viga_variable"])):
            d, cn = max(((flecha(fac), cn) for cn, fac in lista), default=(0.0, ""))
            out.append({"elemento": orig, "tipo": "Viga (vertical)", "combo": cn, "delta_mm": float(d) * 1000,
                        "limite_mm": L / div * 1000, "criterio": f"L/{div:g}", "L": L, "razon": float(d) / (L / div)})

    # ---- pilares: desplazamiento lateral relativo entre sus extremos ----
    def lateral(nodos_ref, nodos_obj, altura_de):
        """máx. |ΔDX|/h (viento: D+W; sismo: E con CQC modo a modo) entre la base y los nudos de nodos_obj."""
        base = lambda c: sum(D(n, "DX", c) for n in nodos_ref) / len(nodos_ref)      # noqa: E731
        res_w, res_e = (0.0, 0.0, ""), (0.0, 0.0)
        for n in nodos_obj:
            h = altura_de(n)
            if h < 0.3:
                continue
            for w in vientos:
                dd = abs(comb(lambda c, n=n: D(n, "DX", c) - base(c), {"DEAD": 1, w: 1}))
                if dd / h > (res_w[0] / res_w[1] if res_w[1] else 0):
                    res_w = (dd, h, f"D+{w}")
            de = float(_cqc([np.array([D(n, "DX", c) - base(c)]) for c in modos], omegas)[0])
            if de / h > (res_e[0] / res_e[1] if res_e[1] else 0):
                res_e = (de, h)
        return res_w, res_e

    def filas_laterales(nombre, tipo, res_w, res_e):
        if res_w[1]:
            d, h, cn = res_w
            out.append({"elemento": nombre, "tipo": f"{tipo} (lateral, viento)", "combo": cn, "delta_mm": float(d) * 1000,
                        "limite_mm": h / lim["lateral_viento"] * 1000, "criterio": f"H/{lim['lateral_viento']:g}", "L": h,
                        "razon": float(d) / (h / lim["lateral_viento"])})
        if res_e[1]:
            d, h = res_e
            out.append({"elemento": nombre, "tipo": f"{tipo} (deriva sísmica)", "combo": "E (espectral, CQC)", "delta_mm": float(d) * 1000,
                        "limite_mm": h * lim["deriva_sismo"] * 1000, "criterio": f"{lim['deriva_sismo']:g}·H (NCh433 5.9.2)",
                        "L": h, "razon": float(d) / (h * lim["deriva_sismo"])})

    pil = {}
    for f, a, b, sec in mod.barras:
        if mod.roles.get(f) == "pilar":
            pil.setdefault(mod.origen.get(f) or f, set()).update((a, b))
    for orig, ns in pil.items():
        bajo = min(ns, key=lambda n: mod.nudos[n][1])
        z0 = mod.nudos[bajo][1]
        alto = max(ns, key=lambda n: mod.nudos[n][1])
        filas_laterales(orig, "Pilar", *lateral([bajo], [alto], lambda n: mod.nudos[n][1] - z0))

    # ---- muros: desplazamiento lateral de los nudos del segmento respecto de su base ----
    for seg in mod.resumen_muros:
        ns = {n for nombre, nn, sec in mod.areas if sec == seg["seccion"] for n in nn}
        if not ns:
            continue
        z0 = min(mod.nudos[n][1] for n in ns)
        base = [n for n in ns if abs(mod.nudos[n][1] - z0) < 1e-6]
        # borde superior del segmento: el nudo más alto de cada vertical (la deriva se mide de la base al borde)
        tope = {}
        for n in ns:
            x = round(mod.nudos[n][0], 4)
            if x not in tope or mod.nudos[n][1] > mod.nudos[tope[x]][1]:
                tope[x] = n
        filas_laterales(f"{seg['muro']} x {seg['x0']}–{seg['x1']}", "Muro",
                        *lateral(base, [n for n in tope.values() if n not in base], lambda n: mod.nudos[n][1] - z0))
    out.sort(key=lambda r: -r["razon"])
    return out
