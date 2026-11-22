"""Verificación de perfiles de acero según AISC 360-16 (LRFD), que adopta la NCh427/1:2016.

Unidades: tonf y m. Perfiles I doblemente simétricos (flexión en el eje fuerte) y cajones rectangulares.
Capítulos: D (tracción), E3/E7 (compresión, con elementos esbeltos aproximados por Q), F2/F3 (flexión de perfiles I,
con volcamiento lateral-torsional), F7 (cajones), G2 (corte) y H1 (interacción).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

E_ACERO = 2.0e8 / 9.80665          # 200 GPa en tonf/m²
FY_A270ES = 270e3 / 9.80665        # 270 MPa en tonf/m² (A270ES, NCh203)


@dataclass
class Perfil:
    forma: str                     # "I" o "Box"
    d: float
    bf: float
    tw: float = 0.0
    tf: float = 0.0
    t: float = 0.0                 # cajón: espesor de pared

    # ---- propiedades geométricas (m) ----
    def props(self) -> dict:
        if self.forma == "Box":
            d, b, t = self.d, self.bf, self.t
            A = d * b - (d - 2 * t) * (b - 2 * t)
            Ix = (b * d ** 3 - (b - 2 * t) * (d - 2 * t) ** 3) / 12
            Iy = (d * b ** 3 - (d - 2 * t) * (b - 2 * t) ** 3) / 12
            Zx = b * d ** 2 / 4 - (b - 2 * t) * (d - 2 * t) ** 2 / 4
            Zy = d * b ** 2 / 4 - (d - 2 * t) * (b - 2 * t) ** 2 / 4
            J = 2 * t * ((b - t) * (d - t)) ** 2 / ((b - t) + (d - t))
            return {"A": A, "Ix": Ix, "Iy": Iy, "Sx": 2 * Ix / d, "Zx": Zx, "Zy": Zy, "J": J, "Cw": 0.0,
                    "rx": math.sqrt(Ix / A), "ry": math.sqrt(Iy / A), "Aw": 2 * (d - 3 * t) * t}
        d, bf, tw, tf = self.d, self.bf, self.tw, self.tf
        h = d - 2 * tf
        A = 2 * bf * tf + h * tw
        Ix = (bf * d ** 3 - (bf - tw) * h ** 3) / 12
        Iy = (2 * tf * bf ** 3 + h * tw ** 3) / 12
        Zx = bf * tf * (d - tf) + tw * h ** 2 / 4
        J = (2 * bf * tf ** 3 + (d - tf) * tw ** 3) / 3
        ho = d - tf
        Cw = Iy * ho ** 2 / 4
        return {"A": A, "Ix": Ix, "Iy": Iy, "Sx": 2 * Ix / d, "Zx": Zx, "J": J, "Cw": Cw, "ho": ho, "h": h,
                "rx": math.sqrt(Ix / A), "ry": math.sqrt(Iy / A), "Aw": d * tw}


def _fcr(Fy: float, E: float, kl_r: float, Q: float = 1.0) -> float:
    """E3 (con Q de E7 para elementos esbeltos)."""
    if kl_r <= 0:
        return Q * Fy
    Fe = math.pi ** 2 * E / kl_r ** 2
    if Q * Fy / Fe <= 2.25:
        return Q * 0.658 ** (Q * Fy / Fe) * Fy
    return 0.877 * Fe


def capacidades(p: Perfil, L_plano: float, L_fuera: float, Lb: float, Fy: float = FY_A270ES, E: float = E_ACERO,
                K: float = 1.0, Cb: float = 1.0) -> dict:
    """Resistencias de diseño φPn (tracción y compresión), φMn (eje fuerte) y φVn, con notas de clasificación."""
    g = p.props()
    notas = []
    raiz = math.sqrt(E / Fy)
    # ---- clasificación de elementos (Tabla B4.1) ----
    Q = 1.0
    if p.forma == "I":
        lam_f, lam_w = p.bf / (2 * p.tf), g["h"] / p.tw
        lp_f, lr_f = 0.38 * raiz, 1.0 * raiz
        lp_w = 3.76 * raiz
        if lam_f > 0.56 * raiz:
            Q *= max(0.5, 0.56 * raiz / lam_f)       # aproximación de E7 para alas esbeltas en compresión
            notas.append(f"ala esbelta en compresión (bf/2tf = {lam_f:.1f})")
        if lam_w > 1.49 * raiz:
            Q *= max(0.5, 1.49 * raiz / lam_w)
            notas.append(f"alma esbelta en compresión (h/tw = {lam_w:.1f})")
    else:
        lam_f, lam_w = (p.bf - 3 * p.t) / p.t, (p.d - 3 * p.t) / p.t
        lp_f, lr_f = 1.12 * raiz, 1.40 * raiz
        lp_w = 2.42 * raiz
        if max(lam_f, lam_w) > 1.40 * raiz:
            Q *= max(0.5, 1.40 * raiz / max(lam_f, lam_w))
            notas.append(f"pared esbelta en compresión (b/t = {max(lam_f, lam_w):.1f})")
    # ---- tracción (D2) y compresión (E3) ----
    phiTn = 0.9 * Fy * g["A"]
    klr = max(K * L_plano / g["rx"], K * L_fuera / g["ry"])
    if klr > 200:
        notas.append(f"esbeltez KL/r = {klr:.0f} > 200")
    phiPn = 0.9 * _fcr(Fy, E, klr, Q) * g["A"]
    # ---- flexión, eje fuerte ----
    Mp = Fy * g["Zx"]
    if p.forma == "I":
        ry, ho, J, Sx, Iy, Cw = g["ry"], g["ho"], g["J"], g["Sx"], g["Iy"], g["Cw"]
        rts = math.sqrt(math.sqrt(Iy * Cw) / Sx)
        Lp = 1.76 * ry * raiz
        Lr = 1.95 * rts * E / (0.7 * Fy) * math.sqrt(J / (Sx * ho) + math.sqrt((J / (Sx * ho)) ** 2 + 6.76 * (0.7 * Fy / E) ** 2))
        if Lb <= Lp:
            Mn_ltb = Mp
        elif Lb <= Lr:
            Mn_ltb = min(Mp, Cb * (Mp - (Mp - 0.7 * Fy * Sx) * (Lb - Lp) / (Lr - Lp)))
        else:
            Fcr = Cb * math.pi ** 2 * E / (Lb / rts) ** 2 * math.sqrt(1 + 0.078 * J / (Sx * ho) * (Lb / rts) ** 2)
            Mn_ltb = min(Mp, Fcr * Sx)
        # F3: pandeo local del ala comprimida
        if lam_f <= lp_f:
            Mn_flb = Mp
        elif lam_f <= lr_f:
            Mn_flb = Mp - (Mp - 0.7 * Fy * Sx) * (lam_f - lp_f) / (lr_f - lp_f)
        else:
            kc = min(max(4 / math.sqrt(g["h"] / p.tw), 0.35), 0.76)
            Mn_flb = 0.9 * E * kc * Sx / lam_f ** 2
            notas.append("ala esbelta en flexión")
        if lam_w > lp_w:
            notas.append(f"alma no compacta en flexión (h/tw = {lam_w:.1f}): falta F4/F5, se usó F2/F3")
        Mn = min(Mn_ltb, Mn_flb)
        detalle_m = f"Lb {Lb:.2f} m (Lp {Lp:.2f}, Lr {Lr:.2f}), Cb {Cb:.2f}"
    else:
        Sx = g["Sx"]
        if lam_f <= lp_f:
            Mn = Mp
        elif lam_f <= lr_f:
            Mn = min(Mp, Mp - (Mp - Fy * Sx) * (3.57 * lam_f * math.sqrt(Fy / E) - 4.0))
        else:
            Mn = Fy * Sx * 0.9
            notas.append("ala del cajón esbelta en flexión (aprox.)")
        detalle_m = "cajón (F7)"
    phiMn = 0.9 * Mn
    # ---- corte (G2.1) ----
    h_tw = (g["h"] / p.tw) if p.forma == "I" else (p.d - 3 * p.t) / p.t
    if p.forma == "I" and h_tw <= 2.24 * raiz:
        phi_v, Cv1 = 1.0, 1.0
    else:
        phi_v = 0.9
        kv = 5.34
        Cv1 = 1.0 if h_tw <= 1.10 * math.sqrt(kv * E / Fy) else 1.10 * math.sqrt(kv * E / Fy) / h_tw
    phiVn = phi_v * 0.6 * Fy * g["Aw"] * Cv1
    return {"phiTn": phiTn, "phiPn": phiPn, "phiMn": phiMn, "phiVn": phiVn, "KL/r": klr, "notas": notas,
            "detalle_flexion": detalle_m}


def razon(Pu: float, Mu: float, Vu: float, cap: dict) -> dict:
    """H1.1 (Pu > 0 compresión, < 0 tracción) y corte: razones de demanda/capacidad."""
    Pc = cap["phiPn"] if Pu >= 0 else cap["phiTn"]
    pr = abs(Pu) / Pc
    mr = abs(Mu) / cap["phiMn"]
    inter = pr + 8 / 9 * mr if pr >= 0.2 else pr / 2 + mr
    return {"interaccion": inter, "corte": abs(Vu) / cap["phiVn"], "ecuacion": "H1-1a" if pr >= 0.2 else "H1-1b"}
