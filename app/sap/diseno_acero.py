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

    def _props_compuesta(self) -> dict:
        """Ángulo L (d, bf, t) y canal C (d, bf, tw, tf; o t): suma de rectángulos; ejes principales para el ángulo."""
        if self.forma == "L":
            t = self.t or self.tw
            rects = [(0, 0, t, self.d), (t, 0, self.bf, t)]                     # (x0, y0, x1, y1)
        else:
            tw, tf = (self.tw or self.t), (self.tf or self.t)
            rects = [(0, 0, tw, self.d), (tw, 0, self.bf, tf), (tw, self.d - tf, self.bf, self.d)]
        A = sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in rects)
        xc = sum((x1 - x0) * (y1 - y0) * (x0 + x1) / 2 for x0, y0, x1, y1 in rects) / A
        yc = sum((x1 - x0) * (y1 - y0) * (y0 + y1) / 2 for x0, y0, x1, y1 in rects) / A
        Ix = Iy = Ixy = 0.0
        for x0, y0, x1, y1 in rects:
            b, h = x1 - x0, y1 - y0
            cx, cy = (x0 + x1) / 2 - xc, (y0 + y1) / 2 - yc
            Ix += b * h ** 3 / 12 + b * h * cy ** 2
            Iy += h * b ** 3 / 12 + b * h * cx ** 2
            Ixy += b * h * cx * cy
        m, rr = (Ix + Iy) / 2, math.sqrt(((Ix - Iy) / 2) ** 2 + Ixy ** 2)
        Imin = m - rr
        J = sum(max(x1 - x0, y1 - y0) * min(x1 - x0, y1 - y0) ** 3 / 3 for x0, y0, x1, y1 in rects)
        Sx = Ix / max(yc, self.d - yc)
        Sy = Iy / max(xc, self.bf - xc)
        out = {"A": A, "Ix": Ix, "Iy": Iy, "Sx": Sx, "Sy": Sy, "J": J, "Smin": min(Sx, Sy)}
        if self.forma == "L":
            r = math.sqrt(Imin / A)
            out.update({"Zx": 1.5 * Sx, "Zy": 1.5 * Sy, "Cw": 0.0, "rx": r, "ry": r, "Aw": self.d * t, "Imin": Imin})
        else:
            h, ho = self.d - 2 * tf, self.d - tf
            Cw = tf * self.bf ** 3 * ho ** 2 / 12 * (3 * self.bf * tf + 2 * ho * tw) / (6 * self.bf * tf + ho * tw)
            Zx = self.bf * tf * (self.d - tf) + tw * h ** 2 / 4
            out.update({"Zx": Zx, "Zy": 1.5 * Sy, "Cw": Cw, "h": h, "ho": ho, "rx": math.sqrt(Ix / A), "ry": math.sqrt(Iy / A),
                        "Aw": self.d * tw})
        return out

    # ---- propiedades geométricas (m) ----
    def props(self) -> dict:
        if self.forma in ("L", "C"):
            return self._props_compuesta()
        if self.forma == "Pipe":                     # tubo circular: d = diámetro exterior, t = espesor
            D, t = self.d, self.t
            Di = D - 2 * t
            A = math.pi / 4 * (D ** 2 - Di ** 2)
            I = math.pi / 64 * (D ** 4 - Di ** 4)
            Z = (D ** 3 - Di ** 3) / 6
            r_ = math.sqrt(I / A)
            return {"A": A, "Ix": I, "Iy": I, "Sx": 2 * I / D, "Zx": Z, "Zy": Z, "J": 2 * I, "Cw": 0.0,
                    "rx": r_, "ry": r_, "Aw": A / 2}
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
    if p.forma == "Pipe":
        dt = p.d / p.t
        if dt > 0.11 * E / Fy:                       # Tabla B4.1a caso 9; E7.2(b)
            Q = min(1.0, 0.038 * E / (Fy * dt) + 2 / 3)
            notas.append(f"tubo esbelto en compresión (D/t = {dt:.0f} > 0,11·E/Fy; E7.2)")
        lam_f = lam_w = dt
        lp_f, lr_f, lp_w = 0.07 * E / Fy, 0.31 * E / Fy, 0.07 * E / Fy
    elif p.forma == "L":
        t = p.t or p.tw
        lam_f = lam_w = max(p.d, p.bf) / t
        lp_f, lr_f, lp_w = 0.54 * raiz, 0.91 * raiz, 0.54 * raiz
        if lam_f > 0.45 * raiz:                         # E7.1 caso c (ala de ángulo)
            Q = 1.34 - 0.76 * lam_f / raiz if lam_f <= 0.91 * raiz else 0.53 / (lam_f / raiz) ** 2
            notas.append(f"ala del ángulo esbelta en compresión (b/t = {lam_f:.1f}; E7)")
    elif p.forma in ("I", "C"):
        tw, tf = (p.tw or p.t), (p.tf or p.t)
        p = Perfil(p.forma, p.d, p.bf, tw, tf, p.t)
        lam_f, lam_w = (p.bf / (2 * p.tf) if p.forma == "I" else p.bf / p.tf), g["h"] / p.tw
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
            notas.append(f"pared esbelta en compresión (b/t = {max(lam_f, lam_w):.1f}; Tabla B4.1a, reduce φPn)")
    # ---- tracción (D2) y compresión (E3) ----
    phiTn = 0.9 * Fy * g["A"]
    klr = max(K * L_plano / g["rx"], K * L_fuera / g["ry"])
    if klr > 200:
        notas.append(f"esbeltez KL/r = {klr:.0f} > 200 (AISC 360-16 E2, nota: recomendación para elementos en compresión)")
    phiPn = 0.9 * _fcr(Fy, E, klr, Q) * g["A"]
    # ---- flexión, eje fuerte ----
    Mp = Fy * g["Zx"]
    if p.forma in ("I", "C"):
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
    if p.forma == "L":                               # F10 (simplificado): fluencia en la fibra más solicitada
        Mn = Fy * g["Smin"]
        detalle_m = "ángulo (F10 simplificado: Mn = Fy·S mín.)"
        notas.append("ángulo: flexión con Mn = Fy·S mín. y pandeo con el radio de giro mínimo (conservador)")
    if p.forma == "Pipe":                            # F8
        Sx = g["Sx"]
        if lam_f <= lp_f:
            Mn = Mp
        elif lam_f <= lr_f:
            Mn = min(Mp, (0.021 * E / lam_f + Fy) * Sx)
        else:
            Mn = min(Mp, 0.33 * E / lam_f * Sx)
            notas.append("tubo esbelto en flexión (F8)")
        detalle_m = "tubo circular (F8)"
    phiMn = 0.9 * Mn
    # ---- corte (G2.1) ----
    h_tw = (g["h"] / p.tw) if p.forma in ("I", "C") else ((max(p.d, p.bf) / (p.t or p.tw)) if p.forma == "L" else (p.d - 3 * p.t) / p.t)
    if p.forma == "Pipe":                            # G5: Fcr = 0,6·Fy (tubos cortos y compactos), Vn = Fcr·Ag/2
        phi_v, Cv1 = 0.9, 1.0
    elif p.forma == "I" and h_tw <= 2.24 * raiz:
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
