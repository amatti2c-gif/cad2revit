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


def analizar(mod, cargas, pc, *, fy: float = FY_A270ES, lb_fuera: float | None = None, n_puntos: int = 9) -> dict:
    """mod: eje_revit.ModeloEje; cargas: eje_cargas.CargasEje; pc: ParametrosCargas."""
    from Pynite import FEModel3D
    from Pynite import Analysis

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
        m.add_material(nombre, E0 * a["f11"], G0 * a["f12"], 0.0, 0.0)
        mat_area[sec] = nombre
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
            m.add_quad(nombre, *ns, t_, mat_area[sec])
            sub[nombre] = [nombre]
            continue
        P = [mod.nudos[n] for n in ns]
        g = nudo_xy(sum(p[0] for p in P) / 3, sum(p[1] for p in P) / 3)
        med = [nudo_xy((P[k][0] + P[(k + 1) % 3][0]) / 2, (P[k][1] + P[(k + 1) % 3][1]) / 2) for k in range(3)]
        sub[nombre] = []
        for k in range(3):
            q = f"{nombre}_{k + 1}"
            m.add_quad(q, ns[k], med[k], g, med[k - 1], t_, mat_area[sec])
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
    m.analyze_linear(check_statics=False)
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
    for f, *_ in mod.barras:
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
    for (orig, sec), fs in por_elemento.items():
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

    return {
        "T": t_est, "periodos": [float(x) for x in periodos], "masa_participante_x": float(sum(m_ef) / masa_tot) if masa_tot else 0.0,
        "corte_basal_espectral": q0, "q_min": q_min, "q_max": q_max, "factor_corte": factor,
        "corte_basal": q0 * factor, "peso_sismico": peso, "espectro": espectro,
        "acero": acero, "madera": madera,
    }
