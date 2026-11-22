"""Verificación de secciones rectangulares de hormigón armado según ACI 318-19 (base de la NCh430).

Unidades: MPa, mm, kN y kN·m. Flexión (22.2, 21.2: φ según εt), corte (22.5: Vc = 0,17·λ·√f'c·b·d sin carga
axial, Vs = Av·fyt·d/s ≤ 0,66·√f'c·b·d) y flexo-compresión por compatibilidad de deformaciones (diagrama P–M).
"""

from __future__ import annotations

import math

ES = 200000.0
ECU = 0.003


def beta1(fc: float) -> float:
    return 0.85 if fc <= 28 else max(0.65, 0.85 - 0.05 * (fc - 28) / 7)


def phi_por_deformacion(et: float, fy: float, espiral: bool = False) -> float:
    ey = fy / ES
    fc_ = 0.75 if espiral else 0.65
    if et <= ey:
        return fc_
    if et >= ey + 0.003:
        return 0.90
    return fc_ + (0.90 - fc_) * (et - ey) / 0.003


def flexion(b: float, h: float, d: float, As: float, fc: float, fy: float, As_comp: float = 0.0, d_comp: float = 0.0) -> dict:
    """Momento resistente φMn de una viga rectangular (con armadura de compresión opcional)."""
    b1 = beta1(fc)
    # se resuelve c por equilibrio con la armadura de compresión en su tensión real
    lo, hi = 1e-3, h
    for _ in range(100):
        c = (lo + hi) / 2
        fs_c = max(-fy, min(fy, ES * ECU * (c - d_comp) / c)) if As_comp else 0.0
        fs_t = max(-fy, min(fy, ES * ECU * (d - c) / c))
        resid = 0.85 * fc * b1 * c * b + As_comp * (fs_c - (0.85 * fc if fs_c > 0 else 0)) - As * fs_t
        if resid > 0:
            hi = c
        else:
            lo = c
    a = b1 * c
    fs_c = max(-fy, min(fy, ES * ECU * (c - d_comp) / c)) if As_comp else 0.0
    Mn = 0.85 * fc * a * b * (d - a / 2) + As_comp * fs_c * (d - d_comp)
    et = ECU * (d - c) / c
    phi = phi_por_deformacion(et, fy)
    rho_min = max(0.25 * math.sqrt(fc) / fy, 1.4 / fy)
    notas = []
    if As < rho_min * b * d:
        notas.append(f"As < As,min = {rho_min * b * d:.0f} mm² (9.6.1.2)")
    if et < 0.004:
        notas.append(f"εt = {et:.4f} < 0,004: no cumple el mínimo para vigas (9.3.3.1)")
    return {"phiMn_kNm": phi * Mn / 1e6, "phi": phi, "c_mm": c, "a_mm": a, "et": et, "notas": notas}


def corte(b: float, d: float, fc: float, fyt: float, Av: float = 0.0, s: float = 0.0, lam: float = 1.0, Nu_kN: float = 0.0,
          Ag: float | None = None) -> dict:
    """φVn = 0,75·(Vc + Vs)."""
    Ag = Ag or b * d
    Vc = (0.17 * lam * math.sqrt(fc) + Nu_kN * 1000 / (6 * Ag)) * b * d
    Vc = max(Vc, 0.0)
    Vs = Av * fyt * d / s if (Av and s) else 0.0
    Vs_max = 0.66 * math.sqrt(fc) * b * d
    notas = []
    if Vs > Vs_max:
        notas.append("Vs supera 0,66·√f'c·b·d: agrandar la sección (22.5.1.2)")
        Vs = Vs_max
    if s and s > min(d / 2, 600):
        notas.append(f"s = {s:.0f} mm > d/2 = {d / 2:.0f} mm (9.7.6.2.2)")
    return {"phiVn_kN": 0.75 * (Vc + Vs) / 1000, "Vc_kN": Vc / 1000, "Vs_kN": Vs / 1000, "notas": notas}


def interaccion(b: float, h: float, capas: list[tuple[float, float]], fc: float, fy: float, n: int = 60,
                espiral: bool = False) -> list[dict]:
    """Diagrama P–M (φPn, φMn) de una columna rectangular con capas de armadura [(As de la capa mm², distancia a la
    fibra comprimida mm)], flexión en torno a un eje; P en kN (+ compresión), M en kN·m."""
    b1 = beta1(fc)
    Ast = sum(a for a, _ in capas)
    Ag = b * h
    Po = 0.85 * fc * (Ag - Ast) + fy * Ast
    Pmax = (0.85 if espiral else 0.80) * Po
    dt = max(dd for _, dd in capas)
    puntos = []
    for i in range(n + 1):
        c = h * 3 * (1 - i / n) + 1e-3                   # de compresión pura a tracción
        a = min(b1 * c, h)
        Cc = 0.85 * fc * a * b
        P = Cc
        M = Cc * (h / 2 - a / 2)
        for As_, di in capas:
            es = ECU * (c - di) / c
            fs = max(-fy, min(fy, ES * es))
            if di < a and fs > 0:
                fs -= 0.85 * fc
            P += As_ * fs
            M += As_ * fs * (h / 2 - di)
        et = ECU * (dt - c) / c
        phi = phi_por_deformacion(et, fy, espiral)
        puntos.append({"phiPn_kN": min(phi * P, 0.65 * Pmax if not espiral else 0.75 * Pmax) / 1000,
                       "phiMn_kNm": phi * M / 1e6})
    # tracción pura
    puntos.append({"phiPn_kN": -0.9 * fy * Ast / 1000, "phiMn_kNm": 0.0})
    return puntos


def dentro_de_interaccion(puntos: list[dict], Pu_kN: float, Mu_kNm: float) -> dict:
    """Razón de demanda: Mu / φMn a la misma carga axial (interpolada en el diagrama)."""
    pts = sorted(puntos, key=lambda q: q["phiPn_kN"])
    if Pu_kN > pts[-1]["phiPn_kN"] or Pu_kN < pts[0]["phiPn_kN"]:
        return {"razon": float("inf"), "phiMn_kNm": 0.0}
    mejor = 0.0
    for a, b in zip(pts, pts[1:]):
        if a["phiPn_kN"] <= Pu_kN <= b["phiPn_kN"] and b["phiPn_kN"] > a["phiPn_kN"]:
            m = a["phiMn_kNm"] + (b["phiMn_kNm"] - a["phiMn_kNm"]) * (Pu_kN - a["phiPn_kN"]) / (b["phiPn_kN"] - a["phiPn_kN"])
            mejor = max(mejor, m)
    return {"razon": abs(Mu_kNm) / mejor if mejor > 0 else float("inf"), "phiMn_kNm": mejor}
