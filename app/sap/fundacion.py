"""Fundación corrida de un eje: pedestal (sobrecimiento) sobre zapata corrida, como viga sobre resortes que solo
trabajan en compresión (Winkler sin tracción), con las reacciones de los apoyos del análisis del eje.

Unidades: tonf y m (el diseño de hormigón se hace en MPa, mm y kN con diseno_hormigon).

- Suelo (NCh433:2026 7.1 y 7.2, combinaciones de servicio de la NCh3171): presión de contacto máxima frente a la
  admisible (estática, o eventual con viento o sismo), % del área en compresión ≥ 80 % (7.2.1) y deslizamiento.
- Hormigón armado (ACI 318-19, combinaciones de resistencia de la NCh3171):
  · viga de fundación (sección T invertida: pedestal + zapata): flexión en ambos sentidos por compatibilidad de
    deformaciones con las capas de barras, y corte con los estribos del pedestal;
  · ala de la zapata en dirección transversal: flexión y corte por metro (voladizo desde la cara del pedestal);
  · pedestal como muro (ACI 318-19 cap. 11): cuantías mínimas, corte en su plano por las fuerzas horizontales de los
    apoyos (11.5.4.3) y carga axial bajo cada apoyo (11.5.3.1 con el largo efectivo de 11.2.3.1).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

G_ACC = 9.80665
TONF_KN = G_ACC                     # 1 tonf = 9,80665 kN


@dataclass
class ArmadurasFundacion:
    """Mismo esquema que la lectura .armaduras.json del botón «Armaduras» de Revit: pedestal y fundación."""
    ped_barras_por_cara: int = 3
    ped_diametro_mm: float = 10.0
    ped_estribo_diametro_mm: float = 8.0
    ped_estribo_separacion_cm: float = 20.0
    ped_estribo_hasta_fondo: bool = True
    fun_barras_superiores: int = 4
    fun_barras_inferiores: int = 4
    fun_diametro_mm: float = 10.0
    fun_estribo_diametro_mm: float = 8.0
    fun_estribo_separacion_cm: float = 20.0


@dataclass
class ParametrosFundacion:
    # geometría (m)
    b_pedestal: float = 0.20
    h_pedestal: float = 0.60            # alto del pedestal sobre la zapata
    b_zapata: float = 0.60
    h_zapata: float = 0.25
    profundidad: float = 0.80           # Df: del terreno al sello de fundación
    voladizo: float = 0.20              # la fundación sigue más allá del primer y último apoyo
    recubrimiento: float = 0.05
    ancho_apoyo: float = 0.20           # placa base o ancho del muro que llega (para la carga axial del pedestal)
    # suelo
    ks: float = 3.0                     # kgf/cm³ (coeficiente de balasto)
    q_adm: float = 15.0                 # tonf/m², presión admisible estática
    q_adm_eventual: float | None = None # tonf/m², con viento o sismo; None = 1,33 · q_adm
    gamma_suelo: float = 1.8            # tonf/m³
    roce: float = 0.40                  # coeficiente de roce suelo-hormigón (deslizamiento)
    # FS mínimo al deslizamiento: 1,0 porque son zapatas corridas conectadas entre sí (el cálculo por eje, sin la
    # continuidad con las fundaciones transversales ni el empuje pasivo, es conservador)
    fs_deslizamiento: float = 1.0
    # materiales
    fc: float = 25.0                    # MPa
    fy: float = 420.0                   # MPa
    gamma_hormigon: float = 2.5         # tonf/m³
    dx: float = 0.10                    # m, largo de los elementos del modelo de la viga
    armaduras: ArmadurasFundacion = field(default_factory=ArmadurasFundacion)


# ------------------------------------------------------------------------------------- viga sobre resortes

def _viga_sobre_resortes(xs: np.ndarray, EI: float, k_lin: float, P: np.ndarray, M: np.ndarray,
                         max_iter: int = 60) -> dict:
    """Viga de Euler-Bernoulli con resortes verticales en los nudos que solo trabajan en compresión.
    P: fuerza vertical en cada nudo (+ hacia arriba), M: momento (+ antihorario). k_lin: tonf/m² (ks·B).
    Devuelve v (+ arriba), q (presión, + compresión, tonf/m), activos y esfuerzos internos por nudo."""
    import scipy.sparse as sp
    import scipy.sparse.linalg as spl
    n = len(xs)
    trib = np.zeros(n)
    trib[:-1] += np.diff(xs) / 2
    trib[1:] += np.diff(xs) / 2
    # matriz de rigidez en banda, armada una vez en formato disperso (2 grados de libertad por nudo)
    filas, cols, vals = [], [], []
    for e in range(n - 1):
        L = xs[e + 1] - xs[e]
        k = EI / L ** 3 * np.array([[12, 6 * L, -12, 6 * L], [6 * L, 4 * L ** 2, -6 * L, 2 * L ** 2],
                                    [-12, -6 * L, 12, -6 * L], [6 * L, 2 * L ** 2, -6 * L, 4 * L ** 2]])
        g = [2 * e, 2 * e + 1, 2 * e + 2, 2 * e + 3]
        for a_ in range(4):
            for b_ in range(4):
                filas.append(g[a_]); cols.append(g[b_]); vals.append(k[a_, b_])
    K = sp.csc_matrix((vals, (filas, cols)), shape=(2 * n, 2 * n))
    F = np.zeros(2 * n)
    F[0::2] = P
    F[1::2] = M
    activos = np.ones(n, bool)
    v = np.zeros(n)
    estable = True
    for _ in range(max_iter):
        resortes = np.zeros(2 * n)
        resortes[np.arange(n)[activos] * 2] = k_lin * trib[activos]
        Kt = (K + sp.diags(resortes)).tocsc()
        try:
            with np.errstate(all="raise"):
                d = spl.spsolve(Kt, F)
            if not np.all(np.isfinite(d)):
                raise np.linalg.LinAlgError
        except (np.linalg.LinAlgError, FloatingPointError, RuntimeError):
            estable = False
            break
        v = d[0::2]
        nuevos = v < 0                                                   # baja = comprime el suelo
        if not nuevos.any():
            estable = False
            break
        if (nuevos == activos).all():
            break
        activos = nuevos
    if not estable or not activos.any():
        return {"estable": False}
    q = np.where(activos, -k_lin * v, 0.0)                               # tonf/m a lo largo de la viga
    # equilibrio vertical: la reacción del suelo debe igualar las cargas (si no, la iteración no convergió)
    if abs(P.sum() + (q * trib).sum()) > 1e-6 * max(np.abs(P).sum(), 1e-9):
        return {"estable": False, "sin_convergencia": True}
    # esfuerzos internos: desde la izquierda, sumando cargas y reacciones del suelo
    R = q * trib                                                         # fuerza del suelo en cada nudo (+ arriba)
    V = np.zeros(n)
    Mi = np.zeros(n)
    acum_v = 0.0
    acum_m = 0.0
    for i in range(n):
        if i > 0:
            acum_m += acum_v * (xs[i] - xs[i - 1])
        acum_v += P[i] + R[i]
        acum_m -= M[i]
        V[i] = acum_v                                                    # corte a la derecha del nudo
        Mi[i] = acum_m                                                   # + tracciona abajo (convención de vigas)
    return {"estable": True, "v": v, "q": q, "activos": activos, "trib": trib, "V": V, "M": Mi}


# ------------------------------------------------------------------------------------- secciones de hormigón

def _mn_seccion(rects, capas, fc, fy, comp_arriba=True):
    """φMn (kN·m) de una sección formada por rectángulos [(b, y_sup, y_inf)] (mm, y hacia abajo desde arriba) con
    capas [(As mm², y mm)], compresión arriba o abajo, carga axial nula (bloque de Whitney)."""
    from .diseno_hormigon import ECU, ES, beta1, phi_por_deformacion
    H = max(r[2] for r in rects)
    if not comp_arriba:                                                  # se refleja la sección
        rects = [(b, H - y2, H - y1) for b, y1, y2 in rects]
        capas = [(a, H - y) for a, y in capas]
    b1 = beta1(fc)

    def concreto(a):
        C, Mc = 0.0, 0.0
        for b, y1, y2 in rects:
            top, bot = y1, min(y2, a)
            if bot > top:
                ar = b * (bot - top)
                C += ar
                Mc += ar * (top + bot) / 2
        return 0.85 * fc * C, 0.85 * fc * Mc

    def equilibrio(c):
        Cc, _ = concreto(b1 * c)
        return Cc + sum(As * max(-fy, min(fy, ES * ECU * (c - y) / c)) for As, y in capas)
    lo, hi = 1e-3, H
    for _ in range(100):
        c = (lo + hi) / 2
        if equilibrio(c) > 0:
            hi = c
        else:
            lo = c
    a = b1 * c
    Cc, Mc_y = concreto(a)
    # par puro (N = 0): Mn = |Σ F·y| con las fuerzas de compresión (+) y tracción (−) a su profundidad y
    Mn = abs(Mc_y + sum(As * max(-fy, min(fy, ES * ECU * (c - y) / c)) * y for As, y in capas))
    dt = max(y for _, y in capas) if capas else H
    et = ECU * (dt - c) / c
    phi = phi_por_deformacion(et, fy)
    return {"phiMn_kNm": phi * Mn / 1e6, "phi": phi, "c_mm": c, "et": et}


def _area_barra(d_mm):
    return math.pi * d_mm ** 2 / 4


# ------------------------------------------------------------------------------------- combinaciones

def _variantes_e(fac: dict) -> list[tuple[str, dict]]:
    """Expande una combinación con E en EX/EZ (100 % / 30 %) y en ambos sentidos."""
    fe = fac.get("E", 0.0)
    base = {k: v for k, v in fac.items() if k != "E"}
    if not fe:
        return [("", base)]
    out = []
    for nombre, ax, az in (("EX+0,3EZ", 1.0, 0.3), ("0,3EX+EZ", 0.3, 1.0)):
        for sx in (1, -1):
            for sz in (1, -1):
                d = dict(base)
                d["EX"] = fe * ax * sx
                d["EZ"] = fe * az * sz
                out.append((f" [{'+' if sx > 0 else '−'}{nombre.replace('EZ', ('EZ' if sz > 0 else '(−EZ)'))}]", d))
    return out


ASD = {"D": {"DEAD": 1.0}, "D+Lr": {"DEAD": 1.0, "LR": 1.0}, "D+W": {"DEAD": 1.0, "W": 1.0},
       "D+0,75W+0,75Lr": {"DEAD": 1.0, "W": 0.75, "LR": 0.75}, "D+E": {"DEAD": 1.0, "E": 1.0},
       "D+0,75E": {"DEAD": 1.0, "E": 0.75}, "0,6D+W": {"DEAD": 0.6, "W": 1.0}, "0,6D+E": {"DEAD": 0.6, "E": 1.0}}


def _expandir(combos: dict, vientos: list[str]) -> list[tuple[str, dict, bool]]:
    """(nombre, factores por caso, es_eventual)."""
    out = []
    for cn, fac in combos.items():
        lista = [fac]
        nombres = [cn]
        if "W" in fac:
            lista = [{**{k: v for k, v in fac.items() if k != "W"}, w: fac["W"]} for w in vientos]
            nombres = [f"{cn} ({w})" for w in vientos]
        for nm, f in zip(nombres, lista):
            for suf, g in _variantes_e(f):
                out.append((nm + suf, g, any(k.startswith("W") or k in ("EX", "EZ") for k in g)))
    return out


# ------------------------------------------------------------------------------------- análisis y diseño

def analizar_fundacion(apoyos: list[dict], combos_lrfd: dict, patrones: dict, pf: ParametrosFundacion, avance=None) -> dict:
    def paso(pct, etapa, hasta=None):
        if avance:
            avance(round(pct), etapa, round(hasta if hasta is not None else pct))
    paso(2, "Armando el modelo de la fundación")
    if not apoyos:
        raise ValueError("El análisis del eje no tiene apoyos: vuelve a analizar el eje")
    vientos = sorted({c for a in apoyos for c in a["casos"] if patrones.get(c) == "WIND"})
    xs_ap = [a["x"] for a in apoyos]
    x0, x1 = min(xs_ap) - pf.voladizo, max(xs_ap) + pf.voladizo
    n_el = max(int(math.ceil((x1 - x0) / pf.dx)), 2)
    malla = np.linspace(x0, x1, n_el + 1)
    paso_m = (x1 - x0) / n_el
    # cada apoyo va a un nudo: si cae cerca de uno de la malla (menos de ¼ del elemento) se usa ese nudo; si no, se
    # agrega. Así no quedan elementos de largo casi nulo (con EI/L³ enorme, la solución pierde el equilibrio)
    extra = [x for x in xs_ap if np.min(np.abs(malla - x)) > 0.25 * paso_m]
    xs = np.unique(np.round(np.concatenate([malla, extra]), 6))
    xs = xs[np.concatenate([[True], np.diff(xs) > 0.2 * paso_m])] if len(xs) > 2 else xs
    L = x1 - x0
    nodo_de = {a["nudo"]: int(np.argmin(np.abs(xs - a["x"]))) for a in apoyos}
    # sección (m)
    bp, hp, B, hz = pf.b_pedestal, pf.h_pedestal, pf.b_zapata, pf.h_zapata
    Ht = hp + hz
    A1, A2 = bp * hp, B * hz
    yc = (A1 * hp / 2 + A2 * (hp + hz / 2)) / (A1 + A2)                  # desde arriba
    I = bp * hp ** 3 / 12 + A1 * (yc - hp / 2) ** 2 + B * hz ** 3 / 12 + A2 * (hp + hz / 2 - yc) ** 2
    Ec = 4700 * math.sqrt(pf.fc) * 1000 / G_ACC                          # tonf/m²
    EI = Ec * I
    k_lin = pf.ks * 1000 * B                                             # kgf/cm³ → tonf/m³, × B → tonf/m²
    peso = pf.gamma_hormigon * (A1 + A2) + pf.gamma_suelo * max(B - bp, 0) * max(pf.profundidad - hz, 0)  # tonf/m
    trib = np.zeros(len(xs))
    trib[:-1] += np.diff(xs) / 2
    trib[1:] += np.diff(xs) / 2

    def cargas(fac):
        """P (+ arriba) y M en cada nudo: −reacciones de los apoyos (el fuerza horizontal da momento en el sello),
        más el peso propio de la fundación y del suelo sobre la zapata (con el factor de D)."""
        P = -fac.get("DEAD", 0.0) * peso * trib
        M = np.zeros(len(xs))
        H = 0.0
        for a in apoyos:
            r = np.zeros(3)
            for c, f in fac.items():
                if c in a["casos"]:
                    r += f * np.array(a["casos"][c])
            i = nodo_de[a["nudo"]]
            P[i] += -r[1]
            M[i] += -r[2] + Ht * r[0]                                    # Fx = −Rx aplicada a Ht sobre el sello
            H += -r[0]
        return P, M, H

    # ---- suelo: combinaciones de servicio ----
    q_ev = pf.q_adm_eventual or 1.33 * pf.q_adm
    servicio = []
    lista_srv = _expandir(ASD, vientos)
    for k_s, (nombre, fac, ev) in enumerate(lista_srv):
        paso(5 + 50 * k_s / len(lista_srv), f"Suelo: combinación de servicio {k_s + 1} de {len(lista_srv)}")
        P, M, H = cargas(fac)
        r = _viga_sobre_resortes(xs, EI, k_lin, P, M)
        if not r["estable"]:
            servicio.append({"combo": nombre, "estable": False, "eventual": ev})
            continue
        largo_comp = float(np.sum(r["trib"][r["activos"]]))
        N = float(np.sum(r["q"] * r["trib"]))
        qmax = float(np.max(r["q"]) / B)
        servicio.append({"combo": nombre, "estable": True, "eventual": ev, "q_max": qmax,
                         "q_adm": q_ev if ev else pf.q_adm, "FU_presion": qmax / (q_ev if ev else pf.q_adm),
                         "pct_compresion": largo_comp / L, "N": N, "H": H,
                         "FS_deslizamiento": (pf.roce * N / abs(H)) if abs(H) > 1e-9 else None,
                         "q": [float(v) / B for v in r["q"]], "asentamiento_max_mm": float(-np.min(r["v"]) * 1000)})

    # ---- hormigón: combinaciones de resistencia ----
    arm = pf.armaduras
    rec = pf.recubrimiento * 1000
    bp_mm, hp_mm, B_mm, hz_mm, Ht_mm = bp * 1000, hp * 1000, B * 1000, hz * 1000, Ht * 1000
    rects = [(bp_mm, 0.0, hp_mm), (B_mm, hp_mm, Ht_mm)]
    capas = []
    n_cara = max(int(arm.ped_barras_por_cara), 0)
    if n_cara:
        # como en el croquis: n barras por cara desde la esquina superior, repartidas hasta la capa superior de la zapata
        y_sup_zap = hp_mm + rec
        ys = [rec + (y_sup_zap - rec) * k / n_cara for k in range(n_cara)]
        capas += [(2 * _area_barra(arm.ped_diametro_mm), y) for y in ys]   # una barra por cara en cada nivel
    capas.append((arm.fun_barras_superiores * _area_barra(arm.fun_diametro_mm), hp_mm + rec))
    capas.append((arm.fun_barras_inferiores * _area_barra(arm.fun_diametro_mm), Ht_mm - rec))
    capas = [(a, y) for a, y in capas if a > 0]
    mn_pos = _mn_seccion(rects, capas, pf.fc, pf.fy, comp_arriba=True)    # tracción abajo
    mn_neg = _mn_seccion(rects, capas, pf.fc, pf.fy, comp_arriba=False)   # tracción arriba
    # corte de la viga con los estribos del pedestal (2 ramas)
    from .diseno_hormigon import corte, flexion
    d_v = Ht_mm - rec if arm.ped_estribo_hasta_fondo else hp_mm - rec
    Av = 2 * _area_barra(arm.ped_estribo_diametro_mm)
    cv = corte(bp_mm, d_v, pf.fc, pf.fy, Av, arm.ped_estribo_separacion_cm * 10)
    # ala de la zapata, por metro: voladizo desde la cara del pedestal, armadura = rama inferior del estribo
    c_ala = max((B - bp) / 2, 0.0)
    d_ala = hz_mm - rec - arm.fun_estribo_diametro_mm
    As_ala = _area_barra(arm.fun_estribo_diametro_mm) / (arm.fun_estribo_separacion_cm * 10) * 1000   # mm²/m
    fl_ala = flexion(1000, hz_mm, d_ala, As_ala, pf.fc, pf.fy, losa=True) if As_ala > 0 else {"phiMn_kNm": 0.0, "notas": []}
    cv_ala = corte(1000, d_ala, pf.fc, pf.fy)

    peor = {"Mpos": (0.0, ""), "Mneg": (0.0, ""), "V": (0.0, ""), "qu": (0.0, ""), "H": (0.0, ""), "P_apoyo": {}}
    lista_lrfd = _expandir(combos_lrfd, vientos)
    for k_l, (nombre, fac, _) in enumerate(lista_lrfd):
        paso(55 + 40 * k_l / len(lista_lrfd), f"Hormigón: combinación mayorada {k_l + 1} de {len(lista_lrfd)}")
        P, M, H = cargas(fac)
        r = _viga_sobre_resortes(xs, EI, k_lin, P, M)
        if not r["estable"]:
            continue
        mmax, mmin = float(np.max(r["M"])), float(np.min(r["M"]))
        vmax = float(np.max(np.abs(r["V"])))
        qu = float(np.max(r["q"]) / B)
        if mmax > peor["Mpos"][0]:
            peor["Mpos"] = (mmax, nombre)
        if -mmin > peor["Mneg"][0]:
            peor["Mneg"] = (-mmin, nombre)
        if vmax > peor["V"][0]:
            peor["V"] = (vmax, nombre)
        if qu > peor["qu"][0]:
            peor["qu"] = (qu, nombre)
        if abs(H) > peor["H"][0]:
            peor["H"] = (abs(H), nombre)
        for a in apoyos:                                                 # compresión que baja por cada apoyo
            rz = sum(f * a["casos"][c][1] for c, f in fac.items() if c in a["casos"])
            if rz > peor["P_apoyo"].get(a["nudo"], (0.0, ""))[0]:
                peor["P_apoyo"][a["nudo"]] = (rz, nombre)

    paso(96, "Diseño del pedestal y de la zapata")
    kN = TONF_KN
    # armadura mínima longitudinal: en la sección rectangular de la zapata sola (B × h_zapata), con sus barras, sin el
    # pedestal (9.6.1.2 con bw = B y el d de cada cara)
    from .diseno_hormigon import opciones_armadura
    rho_min = max(0.25 * math.sqrt(pf.fc) / pf.fy, 1.4 / pf.fy)
    As_zi = arm.fun_barras_inferiores * _area_barra(arm.fun_diametro_mm)
    As_zs = arm.fun_barras_superiores * _area_barra(arm.fun_diametro_mm)
    d_zap = hz_mm - rec - arm.fun_estribo_diametro_mm - arm.fun_diametro_mm / 2

    def minimo_zapata(As, cara, n):
        As_min = rho_min * B_mm * d_zap
        txt = (f"{n} φ{arm.fun_diametro_mm:g} = {As:.0f} mm² frente a As,min = {rho_min:.5f}·{B_mm:.0f}·{d_zap:.0f} = {As_min:.0f} mm² "
               f"(9.6.1.2; sección {B_mm / 10:.0f}×{hz_mm / 10:.0f} cm)")
        if As < As_min:
            txt += f": para cumplir usar {opciones_armadura(As_min)}"
        return {"verificacion": f"Armadura mínima longitudinal de la zapata, cara {cara}", "valor": As / (B_mm * d_zap),
                "minimo": round(rho_min, 5), "razon": As_min / As if As > 0 else None, "notas": [txt]}
    viga = [
        {"verificacion": "Flexión, tracción abajo (Mu+)", "Mu_tonfm": peor["Mpos"][0], "combo": peor["Mpos"][1],
         "phiMn_tonfm": mn_pos["phiMn_kNm"] / kN, "razon": peor["Mpos"][0] * kN / mn_pos["phiMn_kNm"] if mn_pos["phiMn_kNm"] else None},
        {"verificacion": "Flexión, tracción arriba (Mu−)", "Mu_tonfm": peor["Mneg"][0], "combo": peor["Mneg"][1],
         "phiMn_tonfm": mn_neg["phiMn_kNm"] / kN, "razon": peor["Mneg"][0] * kN / mn_neg["phiMn_kNm"] if mn_neg["phiMn_kNm"] else None},
        {"verificacion": "Corte (estribos del pedestal)", "Vu_tonf": peor["V"][0], "combo": peor["V"][1],
         "phiVn_tonf": cv["phiVn_kN"] / kN, "razon": peor["V"][0] * kN / cv["phiVn_kN"], "notas": cv["notas"]},
    ]
    qu = peor["qu"][0]
    Mu_ala = qu * c_ala ** 2 / 2                                         # tonf·m/m
    Vu_ala = qu * max(c_ala - d_ala / 1000, 0.0)
    zapata = [
        minimo_zapata(As_zi, "inferior", arm.fun_barras_inferiores),
        minimo_zapata(As_zs, "superior", arm.fun_barras_superiores),
        {"verificacion": "Ala de la zapata: flexión transversal (por m)", "Mu_tonfm": Mu_ala, "combo": peor["qu"][1],
         "phiMn_tonfm": fl_ala["phiMn_kNm"] / kN, "razon": Mu_ala * kN / fl_ala["phiMn_kNm"] if fl_ala["phiMn_kNm"] else None,
         "notas": fl_ala.get("notas", [])},
        {"verificacion": "Ala de la zapata: corte a d de la cara (por m)", "Vu_tonf": Vu_ala, "combo": peor["qu"][1],
         "phiVn_tonf": cv_ala["phiVn_kN"] / kN, "razon": Vu_ala * kN / cv_ala["phiVn_kN"]},
    ]
    # ---- pedestal como muro (ACI 318-19 cap. 11) ----
    rho_l = 2 * _area_barra(arm.ped_estribo_diametro_mm) / (bp_mm * arm.ped_estribo_separacion_cm * 10)   # vertical
    rho_t = 2 * n_cara * _area_barra(arm.ped_diametro_mm) / (bp_mm * hp_mm) if hp_mm else 0.0          # horizontal
    lam = 1.0
    ac = 0.25 if hp / max(L, 1e-6) <= 1.5 else 0.17
    Acv = bp_mm * L * 1000
    Vn_h = Acv * (ac * lam * math.sqrt(pf.fc) + rho_l * pf.fy) / 1000    # kN (corte horizontal en el plano)
    Vn_h = min(Vn_h, 0.83 * math.sqrt(pf.fc) * Acv / 1000)
    sec_p = f"sección del pedestal {bp_mm / 10:.0f}×{hp_mm / 10:.0f} cm"
    muro = [
        {"verificacion": f"Pedestal como muro: cuantía vertical ρl ≥ 0,0012 (11.6.1; {sec_p})", "valor": rho_l, "minimo": 0.0012,
         "razon": 0.0012 / rho_l if rho_l else None,
         "notas": [f"estribos φ{arm.ped_estribo_diametro_mm:g}@{arm.ped_estribo_separacion_cm:g} (2 ramas) / ({bp_mm:.0f} × separación)"]},
        {"verificacion": f"Pedestal como muro: cuantía horizontal ρt ≥ 0,0020 (11.6.1; {sec_p})", "valor": rho_t, "minimo": 0.0020,
         "razon": 0.0020 / rho_t if rho_t else None,
         "notas": [f"{2 * n_cara} φ{arm.ped_diametro_mm:g} del pedestal / ({bp_mm:.0f} × {hp_mm:.0f})"]},
        {"verificacion": f"Pedestal como muro: corte en su plano por las fuerzas horizontales (11.5.4.3; espesor {bp_mm / 10:.0f} cm)",
         "Vu_tonf": peor["H"][0], "combo": peor["H"][1], "phiVn_tonf": 0.75 * Vn_h / kN,
         "razon": peor["H"][0] * kN / (0.75 * Vn_h) if Vn_h else None},
    ]
    # carga axial bajo cada apoyo: largo efectivo = ancho del apoyo + 4·espesor (11.2.3.1), sin pasar la distancia a
    # los vecinos; φPn = 0,55·φ·f'c·Ag·[1 − (k·lc/32h)²] con k = 0,8 (11.5.3.1)
    lc = hp_mm
    for a in apoyos:
        pu, cn = peor["P_apoyo"].get(a["nudo"], (0.0, ""))
        if pu <= 1e-6 or (a.get("pilar") is None and a.get("tipo") == "x"):
            continue
        vec = sorted(abs(o["x"] - a["x"]) for o in apoyos if o is not a and abs(o["x"] - a["x"]) > 1e-6)
        le = min(pf.ancho_apoyo + 4 * bp, vec[0] if vec else L) * 1000
        phiPn = 0.55 * 0.65 * pf.fc * bp_mm * le * (1 - (0.8 * lc / (32 * bp_mm)) ** 2) / 1000
        muro.append({"verificacion": f"Pedestal como muro: carga axial bajo el apoyo x = {a['x']:.2f} m" + (f" (pilar {a['pilar']})" if a.get("pilar") else ""),
                     "Pu_tonf": pu, "combo": cn, "phiPn_tonf": phiPn / kN, "razon": pu * kN / phiPn if phiPn > 0 else None})

    ok_srv = [s for s in servicio if s["estable"]]
    return {
        "x": [float(v) for v in xs], "L": L, "x0": x0, "x1": x1, "EI": EI, "peso_propio_tonf_m": peso,
        "seccion": {"b_pedestal": bp, "h_pedestal": hp, "b_zapata": B, "h_zapata": hz, "I_m4": I, "yc_m": yc},
        "apoyos": [{"x": a["x"], "nudo": a["nudo"], "pilar": a.get("pilar"), "muro": a.get("muro")} for a in apoyos],
        "servicio": servicio,
        "resumen_suelo": {
            "pct_compresion_min": min((s["pct_compresion"] for s in ok_srv), default=0.0),
            "combo_pct": min(ok_srv, key=lambda s: s["pct_compresion"])["combo"] if ok_srv else "",
            "FU_presion_max": max((s["FU_presion"] for s in ok_srv), default=0.0),
            "combo_presion": max(ok_srv, key=lambda s: s["FU_presion"])["combo"] if ok_srv else "",
            "FS_desliz_min": min((s["FS_deslizamiento"] for s in ok_srv if s["FS_deslizamiento"]), default=None),
            "inestables": [s["combo"] for s in servicio if not s["estable"]],
            "q_adm": pf.q_adm, "q_adm_eventual": q_ev,
            "fs_desliz_exigido": pf.fs_deslizamiento,
            "nota_deslizamiento": ("FS mínimo al deslizamiento 1,0: las fundaciones son zapatas corridas conectadas entre sí en todo el edificio, de modo que el deslizamiento de un eje lo resisten también las fundaciones transversales; el cálculo por eje, sin esa continuidad ni el empuje pasivo del suelo, es conservador" if pf.fs_deslizamiento <= 1.0 + 1e-9
                                   else f"FS mínimo al deslizamiento {pf.fs_deslizamiento:g}".replace(".", ",")),
        },
        "viga": viga, "zapata": zapata, "pedestal": muro,
    }
