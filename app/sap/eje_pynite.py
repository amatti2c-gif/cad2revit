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
from .eje_revit import HOLD_DOWNS, hold_down_para

G_ACC = 9.80665
# versión del formato y del cálculo: un resultado guardado con otra versión se vuelve a calcular al abrir el eje
VERSION_RESULTADO = 9
MASA_OBJETIVO = 0.90            # masa participante mínima en X y en Z (se agregan modos hasta alcanzarla)
MODOS_MAX = 600
# sismo vertical: espectro vertical = 2/3 del horizontal de diseño; combinación de componentes 100 % / 30 %
FACTOR_VERTICAL = 2 / 3
# resistencia admisible al corte de muros (tonf/m), 1 tablero OSB 9,5 mm Sheathing y clavo 8d, según el clavado
# perimetral (Manual de diseño de estructuras en madera, Tablas 45 y 46; SDPWS 4.3A, sismo). Viento: × 1,4 (SDPWS)
VN_OSB_9_5_8D = {150: 0.33, 100: 0.48, 50: 0.79}


def _masa_concentrada(m, peso_nudo):
    import scipy.sparse as sps
    diag = np.zeros(len(m.nodes) * 6)
    for n, w in peso_nudo.items():
        diag[m.nodes[n].ID * 6 + 0] = diag[m.nodes[n].ID * 6 + 1] = w / G_ACC
    return sps.diags(diag).tocsr()


def _masa_consistente(m, mod, peso_barra, peso_quad):
    """Matriz de masa consistente (tonf·s²/m): barras con funciones de forma lineales (axial) y de Hermite
    (transversal y giro), cuadriláteros bilineales integrados con 2×2 puntos de Gauss (DX y DY)."""
    import scipy.sparse as sps
    filas, cols, vals = [], [], []

    def sumar(gdl, Me):
        for i, gi in enumerate(gdl):
            for j, gj in enumerate(gdl):
                if Me[i, j]:
                    filas.append(gi); cols.append(gj); vals.append(Me[i, j])
    for f, a, b, _ in mod.barras:
        mu = peso_barra.get(f, 0.0) / G_ACC
        if mu <= 0:
            continue
        (xa, za), (xb, zb) = mod.nudos[a], mod.nudos[b]
        L = math.hypot(xb - xa, zb - za)
        c, s = (xb - xa) / L, (zb - za) / L
        Ml = np.zeros((6, 6))                                          # local: u1 v1 θ1 u2 v2 θ2
        Ml[np.ix_([0, 3], [0, 3])] = mu * L / 6 * np.array([[2, 1], [1, 2]])
        Ml[np.ix_([1, 2, 4, 5], [1, 2, 4, 5])] = mu * L / 420 * np.array(
            [[156, 22 * L, 54, -13 * L], [22 * L, 4 * L ** 2, 13 * L, -3 * L ** 2],
             [54, 13 * L, 156, -22 * L], [-13 * L, -3 * L ** 2, -22 * L, 4 * L ** 2]])
        Rn = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])              # local → global
        R = np.zeros((6, 6)); R[:3, :3] = Rn; R[3:, 3:] = Rn
        ia, ib = m.nodes[a].ID * 6, m.nodes[b].ID * 6
        sumar([ia, ia + 1, ia + 5, ib, ib + 1, ib + 5], R @ Ml @ R.T)
    gp = 1 / math.sqrt(3)
    for qn, w in peso_quad.items():
        q = m.quads[qn]
        nodos = [q.i_node, q.j_node, q.m_node, q.n_node]
        X = np.array([[n.X, n.Y] for n in nodos])
        Me = np.zeros((4, 4))
        for xi in (-gp, gp):
            for eta in (-gp, gp):
                N = 0.25 * np.array([(1 - xi) * (1 - eta), (1 + xi) * (1 - eta), (1 + xi) * (1 + eta), (1 - xi) * (1 + eta)])
                dN = 0.25 * np.array([[-(1 - eta), (1 - eta), (1 + eta), -(1 + eta)],
                                      [-(1 - xi), -(1 + xi), (1 + xi), (1 - xi)]])
                detJ = abs(np.linalg.det(dN @ X))
                Me += np.outer(N, N) * detJ
        Me *= w / G_ACC
        ids = [n.ID * 6 for n in nodos]
        for k in (0, 1):                                               # DX y DY
            sumar([i + k for i in ids], Me)
    n = len(m.nodes) * 6
    return sps.coo_matrix((vals, (filas, cols)), shape=(n, n)).tocsr()


def _cmax(r: float) -> float:
    """NCh433 Tabla 6.4: Cmax / (S·A0/g) según R."""
    return float(np.interp(r, [2, 3, 4, 5.5, 6, 7], [0.90, 0.60, 0.55, 0.40, 0.35, 0.35]))


def _cqc(valores: list[np.ndarray], omegas: list[float], amort: float = 0.05) -> np.ndarray:
    """Combinación cuadrática completa (Der Kiureghian), vectorizada: √|Σᵢ Σⱼ ρᵢⱼ·vᵢ·vⱼ|."""
    if not len(valores):
        return np.zeros(0)
    V = np.array([np.asarray(v, float) for v in valores])
    w = np.asarray(omegas, float)
    r = w[None, :] / w[:, None]
    rho = 8 * amort ** 2 * (1 + r) * r ** 1.5 / ((1 - r ** 2) ** 2 + 4 * amort ** 2 * r * (1 + r) ** 2)
    plano = V.reshape(len(V), -1)
    tot = np.einsum("ik,ij,jk->k", plano, rho, plano)
    return np.sqrt(np.abs(tot)).reshape(V.shape[1:])


def _s_global(q, combo) -> np.ndarray:
    """Tensiones de membrana en ejes globales en el centro de un cuadrilátero (PyNite 3.2 falla con local=False)."""
    sx, sy, txy = (float(np.ravel(v)[0]) for v in q.membrane(0.0, 0.0, True, combo))
    T = q.T()[:3, :3]
    return T.T @ np.array([[sx, txy, 0.0], [txy, sy, 0.0], [0.0, 0.0, 0.0]]) @ T


def _txy_global(q, combo) -> float:
    return float(_s_global(q, combo)[0, 1])


def analizar(mod, cargas, pc, *, fy: float = FY_A270ES, lb_fuera: float | None = None, n_puntos: int = 9,
             avance=None, limites: dict | None = None, masa: str = "concentrada", ry: float = 1.5) -> dict:
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
    # La matriz de masa se arma aquí desde las mismas cargas de DEAD (PyNite, FEModel3D.M, subestima la masa de las
    # cargas distribuidas en barras): concentrada en los nudos (por defecto, como SAP2000) o consistente.
    peso_nudo: dict = {}                                              # concentrada: tonf por nudo
    peso_barra: dict = {}                                             # consistente: tonf/m a lo largo de cada barra

    def repartir(a, b, w, f=None):
        peso_nudo[a] = peso_nudo.get(a, 0.0) + w / 2
        peso_nudo[b] = peso_nudo.get(b, 0.0) + w / 2
        if f is not None:
            (xa, za), (xb, zb) = mod.nudos[a], mod.nudos[b]
            peso_barra[f] = peso_barra.get(f, 0.0) + w / math.hypot(xb - xa, zb - za)
    for f, a, b, sec in mod.barras:                                   # peso propio del acero
        _, g = perfiles[sec]
        m.add_member_dist_load(f, "FY", -7.85 * g["A"], -7.85 * g["A"], case="DEAD")
        (xa, za), (xb, zb) = mod.nudos[a], mod.nudos[b]
        repartir(a, b, 7.85 * g["A"] * math.hypot(xb - xa, zb - za), f)
    for nombre, ns, sec in mod.areas:                                 # peso de los muros a sus nudos
        a = mod.secciones_area[sec]
        pts = [mod.nudos[n] for n in ns]
        area = abs(sum(pts[k][0] * pts[(k + 1) % len(pts)][1] - pts[(k + 1) % len(pts)][0] * pts[k][1]
                       for k in range(len(pts)))) / 2
        w = mod.materiales[a["material"]]["gamma"] * a["espesor"] * area / len(ns)
        for n in ns:
            m.add_node_load(n, "FY", -w, case="DEAD")
            peso_nudo[n] = peso_nudo.get(n, 0.0) + w
    geo = {f: (mod.nudos[a], mod.nudos[b]) for f, a, b, _ in mod.barras}
    _extremos = {f: (a, b) for f, a, b, _ in mod.barras}
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
        if pat == "DEAD":
            peso = {"GRAV": abs(v) * L, "Z": abs(v) * L, "GRAV_PROY": abs(v) * abs(x1 - x0)}.get(d, 0.0)
            repartir(*_extremos[f], peso, f)
    estaticos = [n for n, t in cargas.patrones.items() if t != "QUAKE"]
    for n in estaticos:
        m.add_load_combo(n, {n: 1.0})

    # ---- modal (masa = DEAD) y espectro DS61 con CQC ----
    paso(5, f"Análisis estático: {len(estaticos)} casos de carga", 75)
    m.analyze_linear(check_statics=False)
    paso(75, "Análisis modal (masa del peso propio)", 80)
    D1_idx, D2_idx, D2 = Analysis._partition_D(m)
    K11, _, _, _ = Analysis._partition(m, m.Ke("DEAD", False, True, sparse=True).tocsr(), D1_idx, D2_idx)
    if masa != "consistente":
        Mg = _masa_concentrada(m, peso_nudo)
    else:
        peso_area = {sec: mod.materiales[a["material"]]["gamma"] * a["espesor"] for sec, a in mod.secciones_area.items()}
        Mg = _masa_consistente(m, mod, peso_barra, {q: peso_area[sec] for nombre, _, sec in mod.areas for q in sub[nombre]})
    M11, _, _, _ = Analysis._partition(m, Mg, D1_idx, D2_idx)
    import scipy.sparse.linalg as sla
    # vectores de influencia: DX (sismo horizontal) y DY (vertical) de los nudos libres
    infl_full = np.zeros(len(m.nodes) * 6)
    infl_z = np.zeros(len(m.nodes) * 6)
    for n in m.nodes.values():
        infl_full[n.ID * 6 + 0] = 1.0
        infl_z[n.ID * 6 + 1] = 1.0
    r = infl_full[D1_idx]
    rz = infl_z[D1_idx]
    Mr, Mrz = M11 @ r, M11 @ rz
    masa_tot, masa_z = float(r @ Mr), float(rz @ Mrz)
    # modos: se agregan (duplicando) hasta que la masa participante llegue al 90 % en X y en Z
    nmax = min(K11.shape[0] - 2, MODOS_MAX)
    nmod = min(max(int(pc.modos), 1), nmax)
    Kc, Mc = K11.tocsc(), M11.tocsc()
    while True:
        vals, vecs = sla.eigsh(Kc, k=nmod, M=Mc, sigma=0.0, which="LM")
        orden = np.argsort(vals)
        vals, vecs = vals[orden], vecs[:, orden]
        mm = np.einsum("ij,ij->j", vecs, np.asarray(M11 @ vecs))
        gam, gam_z = (vecs.T @ Mr) / mm, (vecs.T @ Mrz) / mm
        m_ef, m_ef_z = gam ** 2 * mm, gam_z ** 2 * mm
        part_x = m_ef.sum() / masa_tot if masa_tot else 1.0
        part_z = m_ef_z.sum() / masa_z if masa_z else 1.0
        if (part_x >= MASA_OBJETIVO and part_z >= MASA_OBJETIVO) or nmod >= nmax:
            break
        paso(75, f"Análisis modal: {nmod} modos, masa X {100 * part_x:.0f} %, Z {100 * part_z:.0f} %; agregando modos", 80)
        nmod = min(2 * nmod, nmax)
    omegas = np.sqrt(np.maximum(vals, 1e-12))
    periodos = 2 * math.pi / omegas
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
    # Cada modo se guarda una vez con su forma φ (sin escalar); la respuesta del modo i al sismo horizontal es
    # coef_x[i]·(respuesta a φ) y al vertical coef_z[i]·(respuesta a φ). Se usan los modos que aportan masa.
    # EZ: espectro vertical = 2/3 del horizontal; la masa vertical que no alcanzan los modos va como respuesta
    # estática con Sa(T = 0).
    Sv = lambda T: FACTOR_VERTICAL * Sa(T)                              # noqa: E731
    coef_x_all = np.array([factor * gam[i] * Sa(periodos[i]) / omegas[i] ** 2 for i in range(nmod)])
    coef_z_all = np.array([gam_z[i] * Sv(periodos[i]) / omegas[i] ** 2 for i in range(nmod)])
    aporta = [i for i in range(nmod) if i < pc.modos
              or (masa_tot and m_ef[i] / masa_tot > 1e-4) or (masa_z and m_ef_z[i] / masa_z > 1e-4)]
    modos_combo = []
    for i in aporta:
        nombre = f"_MODO{i + 1}"
        m.add_load_combo(nombre, {})
        Analysis._store_displacements(m, vecs[:, i].reshape(-1, 1), D2 * 0, D1_idx, D2_idx, m.load_combos[nombre])
        modos_combo.append(nombre)
    om_a = [float(omegas[i]) for i in aporta]
    cx, cz = coef_x_all[aporta], coef_z_all[aporta]
    modos_x_esc = list(zip(modos_combo, cx))                              # (combo, coeficiente) para el sismo X
    r_res = rz - vecs @ gam_z                                             # parte de la masa vertical sin modo
    f_res = np.asarray(M11 @ r_res).ravel() * Sv(0.0)
    from scipy.sparse.linalg import spsolve
    d_res = spsolve(K11.tocsc(), f_res).reshape(-1, 1)
    m.add_load_combo("_RESZ", {})
    Analysis._store_displacements(m, d_res, D2 * 0, D1_idx, D2_idx, m.load_combos["_RESZ"])

    def env_e(val):
        """val(combo) → array. Devuelve (EX, EZ, E): EX CQC de los modos horizontales; EZ CQC de los verticales más
        la masa faltante (SRSS); E = máx(EX + 0,3·EZ, 0,3·EX + EZ) (100 % / 30 %). Todo sin signo (±)."""
        resp = [np.asarray(val(c), float) for c in modos_combo]
        ex = _cqc([k * v for k, v in zip(cx, resp)], om_a)
        ez = np.sqrt(_cqc([k * v for k, v in zip(cz, resp)], om_a) ** 2 + np.asarray(val("_RESZ"), float) ** 2)
        return ex, ez, np.maximum(ex + 0.3 * ez, 0.3 * ex + ez)

    # reacciones basales por modo: las fuerzas de inercia M·Γ·φ·Sa (con el factor de corte), cambiadas de signo
    xs_ap = [mod.nudos[n][0] for n in mod.apoyos] or [0.0]
    zs_ap = [mod.nudos[n][1] for n in mod.apoyos] or [0.0]
    ref = (min(xs_ap), min(zs_ap))
    por_id = {n.ID: n for n in m.nodes.values()}

    # reacción basal (ΣFx, ΣFz, ΣMy) = −A·f, con f las fuerzas de inercia en los grados de libertad libres
    gl = np.asarray(D1_idx).ravel().astype(int)
    comp_, nd_ = gl % 6, [por_id[int(g_) // 6] for g_ in gl]
    X_ = np.array([n_.X for n_ in nd_]) - ref[0]
    Y_ = np.array([n_.Y for n_ in nd_]) - ref[1]
    A = np.vstack([(comp_ == 0) * 1.0, (comp_ == 1) * 1.0, -Y_ * (comp_ == 0) + X_ * (comp_ == 1) + (comp_ == 5)])

    def reaccion(fi):
        return -(A @ np.asarray(fi).ravel())
    MF = np.asarray(M11 @ vecs)                                           # M·φ de todos los modos
    reac_modo = list((-(A @ MF) * (factor * gam * np.array([Sa(t) for t in periodos]))).T)
    reac_z = list((-(A @ MF) * (gam_z * np.array([Sv(t) for t in periodos]))).T)
    reac_z_res = reaccion(f_res)
    signo_x = np.sign(reac_modo[i_fund]) if reac_modo else np.ones(3)
    i_fz = int(np.argmax(m_ef_z)) if nmod else 0
    signo_z = np.sign(reac_z[i_fz] if np.abs(reac_z[i_fz]).sum() > np.abs(reac_z_res).sum() else reac_z_res)
    signo_x[signo_x == 0] = 1
    signo_z[signo_z == 0] = 1

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
        cache = {}

        def comp(c, k, f=f):
            if c not in cache:
                cache[c] = fuerzas(f, c)
            return cache[c][k]
        E = tuple(env_e(lambda c, k=k: comp(c, k))[2] for k in range(3))
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
                    Pu = Nk                                                # PyNite: axial + = compresión
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
    columna_fuerte = _columna_fuerte(mod, perfiles, res_barra, combos_lrfd, estaticos, fy, ry)

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

        def V_fila(combo, comp=(0, 1)):
            v = 0.0
            for nombre, ns in fila:
                xs_ = [mod.nudos[n][0] for n in ns]
                txy = sum(_s_global(m.quads[q], combo)[comp] for q in sub[nombre]) / len(sub[nombre])
                v += txy * t * (max(xs_) - min(xs_))
            return v
        Vest = {c: V_fila(c) for c in ["DEAD", "LR"] + vientos}
        # carga gravitacional que baja por el segmento (compresión a media altura, tonf): estabiliza el volcamiento
        Nest = {c: -V_fila(c, (1, 1)) for c in ("DEAD", "LR")}
        VE = float(env_e(lambda c: np.array([V_fila(c)]))[2][0])
        # sismo vertical: resta (o suma) carga gravitacional al segmento; para el volcamiento se toma la resta
        NEz = float(env_e(lambda c: np.array([V_fila(c, (1, 1))]))[1][0])
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
        # ---- anclaje al volcamiento (Manual 3.30 y 3.31): T = V·H/L' − (carga gravitacional)·L/(2·L') ----
        anc = seg.get("anclaje") or {"bordes": ["hold_down", "hold_down"], "hold_down": "auto"}
        b_ext = float(pm.get("b_ext", 0.10))
        b_hd = HOLD_DOWNS.get(anc.get("hold_down"), HOLD_DOWNS["HD12"])[2]
        Lp = max(L - 1.5 * b_ext - b_hd, 0.3)
        peor_t = (0.0, "")
        for cn, fac in asd.items():
            for w in (vientos if "W" in fac else [None]):
                V = abs(sum(fac.get(c, 0) * Vest[c] for c in ("DEAD", "LR")) + (fac.get("W", 0) * Vest[w] if w else 0.0)) \
                    + fac.get("E", 0) * VE
                Ngrav = max(sum(fac.get(c, 0) * Nest[c] for c in ("DEAD", "LR")) - fac.get("E", 0) * NEz, 0.0)
                T = V * H / Lp - Ngrav * L / (2 * Lp)
                if T > peor_t[0]:
                    peor_t = (T, cn + (f" ({w})" if w else ""))
        T, combo_t = peor_t
        anclajes = []
        for lado, tipo in zip(("izquierdo", "derecho"), anc["bordes"]):
            if tipo == "pilar":
                cap = anc.get("t_union_pilar")
                anclajes.append({"borde": lado, "tipo": "unión al pilar", "modelo": "PPDD apernado al pilar",
                                 "T_adm": cap, "FU": (T / cap) if cap else None})
            else:
                modelo = anc.get("hold_down")
                if modelo not in HOLD_DOWNS:
                    modelo = hold_down_para(T) or "ninguno alcanza"
                t_adm = HOLD_DOWNS[modelo][0] if modelo in HOLD_DOWNS else None
                anclajes.append({"borde": lado, "tipo": "hold-down", "modelo": modelo, "T_adm": t_adm,
                                 "FU": (T / t_adm) if t_adm else float("inf")})
        fu_anc = max((a["FU"] for a in anclajes if a["FU"] is not None), default=0.0)
        madera.append({"muro": seg["muro"], "x0": x0, "x1": x1, "L": L, "H": H, **peor, "T_anclaje": T,
                       "combo_anclaje": combo_t, "L_prima": Lp, "anclajes": anclajes, "FU_anclaje": fu_anc,
                       "sin_traccion": T <= 1e-9,
                       "falta_capacidad_union": T > 1e-9 and any(a["tipo"] == "unión al pilar" and not a["T_adm"] for a in anclajes)})
    madera.sort(key=lambda r: -max(r["FU"], r["FU_anclaje"] if math.isfinite(r["FU_anclaje"]) else 1e9))

    deformaciones = _deformaciones(m, mod, cargas, modos_x_esc, om_a, sub, {**LIMITES_DEF, **(limites or {})})
    sismo = {"reac_x": _cqc(reac_modo, list(omegas)) if reac_modo else np.zeros(3), "signo_x": signo_x,
             "reac_z": np.sqrt(_cqc(reac_z, list(omegas)) ** 2 + reac_z_res ** 2) if reac_z else np.abs(reac_z_res),
             "signo_z": signo_z, "env": env_e, "masa_z": float(m_ef_z.sum() / masa_z) if masa_z else 0.0}
    casos = _casos_puros(m, mod, cargas, estaticos, res_barra, modos_combo, list(omegas), factor, sismo, ref)
    # visor: los primeros modos y los que aportan al menos 1 % de masa en X o Z (máx. 40)
    pxs = m_ef / masa_tot if masa_tot else m_ef * 0
    pzs = m_ef_z / masa_z if masa_z else m_ef_z * 0
    ver = [i for i in aporta if i < max(pc.modos, 12) or pxs[i] >= 0.01 or pzs[i] >= 0.01][:40]
    visor = _visor(m, mod, cargas, estaticos, [(f"_MODO{i + 1}", i) for i in ver], periodos, pxs, pzs)

    return {
        "T": t_est, "periodos": [float(x) for x in periodos], "masa_participante_x": float(sum(m_ef) / masa_tot) if masa_tot else 0.0,
        "corte_basal_espectral": q0, "q_min": q_min, "q_max": q_max, "factor_corte": factor,
        "corte_basal": q0 * factor, "peso_sismico": peso, "espectro": espectro, "masa": masa,
        "masa_participante_z": sismo["masa_z"], "factor_vertical": FACTOR_VERTICAL, "n_modos": int(nmod),
        "n_modos_usados": len(aporta),
        "criterio_sismo": "E = máx(EX + 0,3·EZ ; 0,3·EX + EZ), en ambos sentidos (±); EX: espectro NCh433 + DS61 con CQC; "
                          "EZ: espectro vertical = 2/3 del horizontal, CQC + masa vertical no captada por los modos (Sa(0))",
        "version": VERSION_RESULTADO, "visor": visor, "columna_fuerte": columna_fuerte, "ry": ry, "acero": acero, "madera": madera, "deformaciones": deformaciones, "casos": casos,
        "limites_deformacion": {**LIMITES_DEF, **(limites or {})},
    }


# límites de deformación por defecto (servicio, cargas sin mayorar):
# - vigas y costaneras de techo, flecha relativa a la cuerda: L/240 con cargas totales (D+Lr, D+W, D+0,75Lr+0,75W) y
#   L/360 con la carga variable sola (Lr o W)  [criterio usual, AISC Design Guide 3 / IBC Tabla 1604.3]
# - pilares y muros, desplazamiento lateral relativo: H/200 con viento (D+W) y 0,002·H con sismo (NCh433 5.9.2, con
#   los desplazamientos del análisis espectral reducido)
LIMITES_DEF = {"viga_total": 240, "viga_variable": 360, "lateral_viento": 200, "deriva_sismo": 0.002}


def _columna_fuerte(mod, perfiles, res_barra, combos, estaticos, fy, ry) -> list[dict]:
    """Columna fuerte–viga débil, AISC 341-16 E3.4a (marcos especiales a momento), en cada nudo donde se unen pilares
    y vigas: ΣM*pc / ΣM*pb > 1,0 con M*pc = Zc·(Fyc − Puc/Ag) y M*pb = 1,1·Ry·Fyb·Zb (sin el término Muv por la
    excentricidad de la rótula). Puc: la mayor compresión del pilar en las combinaciones con sismo."""
    inc = {}
    for f, a, b, sec in mod.barras:
        rol = mod.roles.get(f)
        if rol in ("pilar", "viga", "techo"):
            for n, extremo in ((a, 0), (b, -1)):
                inc.setdefault(n, []).append((f, sec, rol, extremo))
    con_e = {cn: fac for cn, fac in combos.items() if fac.get("E")}
    out = []
    for n, lista in inc.items():
        pil = [x for x in lista if x[2] == "pilar"]
        vig = [x for x in lista if x[2] in ("viga", "techo")]
        if not pil or not vig:
            continue
        x, z = mod.nudos[n]
        smpc, smpb, detalle, p_rel = 0.0, 0.0, [], 0.0
        for f, sec, _, ext in pil:
            g = perfiles[sec][1]
            est, E = res_barra[f]
            puc = 0.0
            for fac in con_e.values():
                N = sum(fac.get(c, 0) * est[c][0][ext] for c in estaticos if c in fac)
                puc = max(puc, N + fac["E"] * E[0][ext])                   # PyNite: axial + = compresión
            smpc += g["Zx"] * max(fy - puc / g["A"], 0.0)
            p_rel = max(p_rel, puc / (fy * g["A"]))
            detalle.append(f"{mod.origen.get(f) or f} ({sec}, Puc {puc:.2f} tonf)")
        for f, sec, _, _ in vig:
            g = perfiles[sec][1]
            smpb += 1.1 * ry * fy * g["Zx"]
            detalle.append(f"{mod.origen.get(f) or f} ({sec})")
        tope = not any(mod.nudos[o][1] > z + 1e-6 for f, sec, _, ext in pil
                       for o in [next(bb if ext == 0 else aa for ff, aa, bb, _ in mod.barras if ff == f)])
        razon = smpc / smpb if smpb else float("inf")
        out.append({"nudo": n, "x": x, "z": z, "pilares": [mod.origen.get(f) or f for f, *_ in pil],
                    "vigas": [mod.origen.get(f) or f for f, *_ in vig], "sum_Mpc": float(smpc), "sum_Mpb": float(smpb), "razon": float(razon),
                    "cumple": bool(razon > 1.0), "Puc_rel": float(p_rel),
                    "excepcion": bool(tope and p_rel < 0.3),
                    "detalle": "; ".join(detalle)})
    out.sort(key=lambda r: r["razon"])
    return out


def _visor(m, mod, cargas, estaticos, modos, periodos, part, part_z=None) -> dict:
    """El modelo tal como quedó en PyNite, para dibujarlo en la app: nudos (incluidos los internos de los triángulos),
    barras, cuadriláteros, apoyos, cargas distribuidas por caso, deformadas de cada caso y formas modales (normalizadas)."""
    r6 = lambda v: round(float(v), 6)                                 # noqa: E731
    nudos = {n.name: [r6(n.X), r6(n.Y)] for n in m.nodes.values()}

    def campo(c, norm=False):
        d = {n.name: [n.DX[c], n.DY[c]] for n in m.nodes.values()}
        k = max((max(abs(a), abs(b)) for a, b in d.values()), default=0.0)
        k = (1 / k) if (norm and k) else 1.0
        return {n: [float(f"{a * k:.4g}"), float(f"{b * k:.4g}")] for n, (a, b) in d.items()}
    carg = {}
    for f, pat, d, v in cargas.distribuidas:
        carg.setdefault(pat, []).append([f, d, r6(v)])
    for f, a, b, sec in mod.barras:                                   # peso propio del acero (DEAD)
        carg.setdefault("DEAD", []).append([f, "GRAV", r6(7.85 * m.sections[sec].A)])
    return {
        "nudos": nudos,
        "barras": [[f, a, b, mod.origen.get(f), mod.roles.get(f), sec] for f, a, b, sec in mod.barras],
        "quads": [[q.name, [q.i_node.name, q.j_node.name, q.m_node.name, q.n_node.name]] for q in m.quads.values()],
        "apoyos": dict(mod.apoyos),
        "cargas": carg,
        "deformadas": {c: campo(c) for c in estaticos},
        "modos": [{"n": i + 1, "T": float(periodos[i]), "masa_x": float(part[i]),
                   "masa_z": float(part_z[i]) if part_z is not None else 0.0, "forma": campo(c, True)} for c, i in modos],
    }


def _casos_puros(m, mod, cargas, estaticos, res_barra, modos, omegas, factor, sismo, ref) -> list[dict]:
    """Resumen de cada estado de carga sin mayorar: reacciones totales, desplazamientos máximos, esfuerzos máximos en
    las barras de acero y las cargas aplicadas a cada elemento de Revit (tonf/m)."""
    desc = {"DEAD": "Peso propio (acero y muros) + peso del techo", "LR": "Sobrecarga de techo Lr (NCh1537)"}
    for linea in cargas.resumen:
        t = linea.strip()
        if t[:1] == "W" and ":" in t and t.split(":")[0][1:].isdigit():
            desc[t.split(":")[0]] = "Viento NCh432: " + t.split(":", 1)[1].strip()
    apoyos = list(mod.apoyos)
    nudos = list(mod.nudos)
    # cargas aplicadas por elemento de Revit, patrón y dirección: rango de valores (tonf/m)
    aplic = {}
    for f, pat, d, v in cargas.distribuidas:
        k = (pat, mod.origen.get(f) or f, mod.roles.get(f) or "", {"X": "horizontal (X)", "Z": "vertical (Z, + arriba)",
                                                                     "GRAV": "gravedad", "GRAV_PROY": "gravedad, por m de proyección"}[d])
        a = aplic.setdefault(k, [v, v])
        a[0], a[1] = min(a[0], v), max(a[1], v)
    out = []
    for c in estaticos:
        rx = sum(m.nodes[n].RxnFX[c] for n in apoyos)
        ry = sum(m.nodes[n].RxnFY[c] for n in apoyos)
        mz = sum(m.nodes[n].RxnMZ[c] + (mod.nudos[n][0] - ref[0]) * m.nodes[n].RxnFY[c]
                 - (mod.nudos[n][1] - ref[1]) * m.nodes[n].RxnFX[c] for n in apoyos)
        dx = max((abs(m.nodes[n].DX[c]) for n in nudos), default=0.0)
        dy = max((abs(m.nodes[n].DY[c]) for n in nudos), default=0.0)
        N = max((float(np.max(np.abs(e[c][0]))) for e, _ in res_barra.values()), default=0.0)
        V = max((float(np.max(np.abs(e[c][1]))) for e, _ in res_barra.values()), default=0.0)
        M = max((float(np.max(np.abs(e[c][2]))) for e, _ in res_barra.values()), default=0.0)
        cargas_el = [{"elemento": o, "rol": rol, "direccion": d, "min": a[0], "max": a[1]}
                     for (pat, o, rol, d), a in aplic.items() if pat == c]
        out.append({"caso": c, "tipo": cargas.patrones.get(c, ""), "descripcion": desc.get(c, ""),
                    "reaccion_x": float(rx), "reaccion_y": float(ry), "momento": float(mz), "dx_max_mm": float(dx) * 1000, "dy_max_mm": float(dy) * 1000,
                    "N_max": N, "V_max": V, "M_max": M, "cargas": cargas_el})
    # sismo: horizontal EX y vertical EZ, cada uno en ambos sentidos. Reacciones: CQC (sin signo) de las de cada modo,
    # con el signo del modo dominante de esa dirección
    env = sismo["env"]
    nodo = lambda n, gdl: (lambda c: np.array([getattr(m.nodes[n], gdl)[c]]))     # noqa: E731
    dx_e = max((float(env(nodo(n, "DX"))[0][0]) for n in nudos), default=0.0)
    dy_ez = max((float(env(nodo(n, "DY"))[1][0]) for n in nudos), default=0.0)
    ee = [e for _, e in res_barra.values()]
    for nombre, clave, sg, desc_ in (
            ("EX", "reac_x", "signo_x", f"Sismo horizontal NCh433 + DS61, espectral CQC (factor de corte {factor:.2f})"),
            ("EZ", "reac_z", "signo_z", "Sismo vertical: espectro = 2/3 del horizontal, CQC + masa no captada por los modos")):
        r_ = sismo[clave] * sismo[sg]
        for s_, txt in ((1, "+"), (-1, "−")):
            out.append({"caso": nombre + txt, "tipo": "QUAKE", "descripcion": desc_ + f", sentido {txt}",
                        "reaccion_x": float(s_ * r_[0]), "reaccion_y": float(s_ * r_[1]), "momento": float(s_ * r_[2]),
                        "dx_max_mm": dx_e * 1000 if nombre == "EX" else None,
                        "dy_max_mm": dy_ez * 1000 if nombre == "EZ" else None,
                        "N_max": None, "V_max": None, "M_max": None, "cargas": []})
    out.append({"caso": "E", "tipo": "QUAKE", "descripcion": "Sismo de diseño: máx(EX + 0,3·EZ ; 0,3·EX + EZ), ±",
                "reaccion_x": None, "reaccion_y": None, "momento": None, "envolvente": True, "dx_max_mm": None, "dy_max_mm": None,
                "N_max": max((float(np.max(e[0])) for e in ee), default=0.0), "V_max": max((float(np.max(e[1])) for e in ee), default=0.0),
                "M_max": max((float(np.max(e[2])) for e in ee), default=0.0), "cargas": []})
    return out


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
            de = float(_cqc([np.array([k * (D(n, "DX", c) - base(c))]) for c, k in modos], omegas)[0])
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
