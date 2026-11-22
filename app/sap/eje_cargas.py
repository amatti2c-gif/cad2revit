"""Cargas y combinaciones del modelo 2D de un eje (tonf, m), según las normas chilenas:

- Peso propio y peso del techo (D).
- Sobrecarga de techo Lr, NCh1537:2009 §5.3.2 y §8.2 (techo accesible solo para mantención, Lo = 1 kPa reducida por
  área tributaria R1 y pendiente R2, mínimo 0,3 kPa).
- Viento W, NCh432:2010 para el sistema principal (MWFRS) con el viento en el plano del eje (perpendicular a la
  cumbrera): q_h = 0,613·Kz·Kzt·Kd·V²·I [N/m²], p = q_h·(G·Cp − GCpi). Coeficientes Cp de muros y techo en el formato
  del procedimiento analítico (mismo de ASCE 7-05, base de la NCh432:2010; verificar con las tablas de la norma).
  Dos presiones internas (±GCpi) y las dos direcciones (+X, −X).
- Sismo E, NCh433 con el espectro del DS61: Sa = S·A0·α/(R*/I), α = (1 + 4,5(Tn/T0)^p)/(1 + (Tn/T0)³),
  R* = 1 + T*/(0,10·T0 + T*/R0).
- Combinaciones por resistencia de la NCh3171:2010 §9.1.1 (sin L de pisos, sin nieve).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

G_ACC = 9.80665

# DS61 (NCh433): tipo de suelo → (S, T0, T', n, p)
SUELOS_DS61 = {
    "A": (0.90, 0.15, 0.20, 1.00, 2.0),
    "B": (1.00, 0.30, 0.35, 1.33, 1.5),
    "C": (1.05, 0.40, 0.45, 1.40, 1.6),
    "D": (1.20, 0.75, 0.85, 1.80, 1.0),
    "E": (1.30, 1.20, 1.35, 1.80, 1.0),
}
A0_ZONA = {1: 0.20, 2: 0.30, 3: 0.40}           # en g

# exposición → (alfa, zg [m]) del perfil de Kz
EXPOSICION = {"B": (7.0, 365.76), "C": (9.5, 274.32), "D": (11.5, 213.36)}

# Cp del techo a barlovento (h/L ≤ 0,25): ángulo → (succión, presión); sotavento: ángulo → Cp
CP_TECHO_BARLOVENTO = [(0, -0.9, -0.18), (10, -0.7, -0.18), (15, -0.5, 0.0), (20, -0.3, 0.2), (25, -0.2, 0.3),
                       (30, -0.2, 0.3), (35, 0.0, 0.4), (45, 0.0, 0.4)]
CP_TECHO_SOTAVENTO = [(0, -0.3), (10, -0.3), (15, -0.5), (20, -0.6), (90, -0.6)]


@dataclass
class ParametrosCargas:
    peso_techo: float = 0.03            # tonf/m² de techo (cubierta, costaneras, aislación)
    lo_techo: float = 0.10              # tonf/m², NCh1537 Tabla 4: techo con acceso solo para mantención (1 kPa)
    reducir_lr: bool = True             # NCh1537 §8.2: reducción por área tributaria (R1) y pendiente (R2)
    # viento
    v_viento: float = 35.0              # m/s, velocidad básica del lugar (NCh432)
    exposicion: str = "C"
    kzt: float = 1.0
    kd: float = 0.85
    importancia_viento: float = 1.0
    g_rafaga: float = 0.85
    gcpi: float = 0.18                  # edificio cerrado
    cp_barlovento_muro: float = 0.8
    cp_sotavento_muro: float = -0.5
    # sismo
    zona: int = 2
    suelo: str = "C"
    importancia_sismo: float = 1.0      # categoría II
    r0: float = 7.0                     # NCh433 Tabla 5.1: muros y sistemas arriostrados de madera
    modos: int = 12


def reduccion_lr(area_tributaria: float, pendiente_pct: float) -> dict:
    """NCh1537 §8.2: factores R1 (área tributaria A_T, m²) y R2 (pendiente F, %) y el factor aplicado a Lo."""
    r1 = 1 - 0.008 * area_tributaria if area_tributaria < 50 else 0.6
    r2 = 1 - 0.0233 * pendiente_pct if pendiente_pct < 30 else 0.3
    r = r1 * r2
    f = 1.0 if r >= 0.84 else (r if r > 0.3 else 0.3)
    return {"R1": r1, "R2": r2, "R1R2": r, "factor": f}


def lr_techo(p: ParametrosCargas, area_tributaria: float, pendiente_pct: float) -> float:
    """Sobrecarga de techo [tonf/m² de proyección horizontal]: Lo, reducida según NCh1537 §8.2 si se pide; nunca
    menos de 0,3 kPa (§5.3.2)."""
    f = reduccion_lr(area_tributaria, pendiente_pct)["factor"] if p.reducir_lr else 1.0
    return max(p.lo_techo * f, 0.03)


def kz(p: ParametrosCargas, z: float) -> float:
    alfa, zg = EXPOSICION[p.exposicion]
    return 2.01 * (max(z, 4.57) / zg) ** (2 / alfa)


def q_viento(p: ParametrosCargas, h: float) -> float:
    """Presión de velocidad a la altura media del techo h [tonf/m²]."""
    return 0.613 * kz(p, h) * p.kzt * p.kd * p.v_viento ** 2 * p.importancia_viento / 1000.0 / G_ACC


def _interp(tabla, ang, col):
    for a, b in zip(tabla, tabla[1:]):
        if a[0] <= ang <= b[0]:
            return a[col] + (b[col] - a[col]) * (ang - a[0]) / (b[0] - a[0])
    return tabla[-1][col]


def espectro_ds61(p: ParametrosCargas, t_estrella: float, periodos=None) -> list[tuple[float, float]]:
    """Espectro de diseño NCh433 + DS61 (Sa/g) para el periodo fundamental T*."""
    s, t0, _, _, pp = SUELOS_DS61[p.suelo]
    a0 = A0_ZONA[p.zona]
    r_est = 1 + t_estrella / (0.10 * t0 + t_estrella / p.r0)
    periodos = periodos or [0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
    out = []
    for tn in periodos:
        alfa = (1 + 4.5 * (tn / t0) ** pp) / (1 + (tn / t0) ** 3)
        out.append((tn, s * a0 * alfa / (r_est / p.importancia_sismo)))
    return out


@dataclass
class CargasEje:
    patrones: dict = field(default_factory=dict)       # nombre → tipo (DEAD, ROOF LIVE, WIND, QUAKE)
    distribuidas: list = field(default_factory=list)   # (barra, patrón, dir "X"|"Z"|"GRAV"|"GRAV_PROY", valor tonf/m)
    combos: dict = field(default_factory=dict)         # nombre → {caso: factor}
    resumen: list = field(default_factory=list)


def cargas_eje(mod, p: ParametrosCargas | None = None) -> CargasEje:
    """Cargas sobre las barras del modelo (mod: eje_revit.ModeloEje) y combinaciones NCh3171."""
    p = p or ParametrosCargas()
    c = CargasEje()
    b = mod.ancho_tributario or 1.0
    if not mod.ancho_tributario:
        c.resumen.append("Sin ancho tributario en el archivo: se usó 1 m")
    c.patrones = {"DEAD": "DEAD", "LR": "ROOF LIVE", "W1": "WIND", "W2": "WIND", "W3": "WIND", "W4": "WIND", "E": "QUAKE"}

    def geo(f):
        _, a, bb, _ = next(x for x in mod.barras if x[0] == f)
        (x0, z0), (x1, z1) = mod.nudos[a], mod.nudos[bb]
        return x0, z0, x1, z1

    techo = [f for f, r in mod.roles.items() if r == "techo"]
    pilares = [f for f, r in mod.roles.items() if r == "pilar"]
    zs = [z for _, z in mod.nudos.values()]
    z_base, z_alto = min(zs), max(zs)
    # altura media del techo y pendiente
    z_alero = min((min(geo(f)[1], geo(f)[3]) for f in techo), default=z_alto)
    h = max((z_alero + z_alto) / 2 - z_base, 0.1)
    proy = sum(abs(geo(f)[2] - geo(f)[0]) for f in techo)
    pend = max((abs(geo(f)[3] - geo(f)[1]) / max(abs(geo(f)[2] - geo(f)[0]), 1e-6) for f in techo), default=0.0)
    ang = math.degrees(math.atan(pend))

    # D: peso del techo (por m² de cubierta, a lo largo de la barra)
    for f in techo:
        c.distribuidas.append((f, "DEAD", "GRAV", p.peso_techo * b))
    # Lr: por m² de proyección horizontal; área tributaria de cada faldón ≈ proyección del faldón × ancho tributario
    at = proy / 2 * b if ang > 1 else proy * b
    lr = lr_techo(p, at, pend * 100)
    for f in techo:
        c.distribuidas.append((f, "LR", "GRAV_PROY", lr * b))
    red = reduccion_lr(at, pend * 100)
    if p.reducir_lr:
        c.resumen.append(f"Lr = {lr * 1000:.0f} kgf/m² — NCh1537 §8.2: Lo {p.lo_techo * 1000:.0f} kgf/m², "
                         f"A_T {at:.1f} m² → R1 = {red['R1']:.3f}; pendiente F {pend * 100:.1f} % → R2 = {red['R2']:.3f}; "
                         f"R1·R2 = {red['R1R2']:.3f} → factor {red['factor']:.3f}" + (" (mínimo 0,3 kPa)" if lr > p.lo_techo * red['factor'] + 1e-9 else ""))
    else:
        c.resumen.append(f"Lr = {lr * 1000:.0f} kgf/m² — sin reducción (Lo); con NCh1537 §8.2 sería factor "
                         f"{red['factor']:.3f} (R1 {red['R1']:.3f}, R2 {red['R2']:.3f})")

    # W: viento en el plano del eje, +X (W1 con +GCpi, W2 con −GCpi) y −X (W3, W4)
    qh = q_viento(p, h)
    c.resumen.append(f"Viento: q_h = {qh * 1000:.1f} kgf/m² (V {p.v_viento} m/s, exposición {p.exposicion}, h {h:.2f} m); "
                     f"pendiente {ang:.1f}°")
    x_min = min((geo(f)[0] for f in pilares), default=None)
    x_max = max((geo(f)[0] for f in pilares), default=None)
    for nombre, sentido, gcpi in (("W1", 1, p.gcpi), ("W2", 1, -p.gcpi), ("W3", -1, p.gcpi), ("W4", -1, -p.gcpi)):
        # muros: presiones netas horizontales sobre los pilares extremos (la interna se anula entre ambos muros)
        for f in pilares:
            x = geo(f)[0]
            if x_min is not None and abs(x - (x_min if sentido > 0 else x_max)) < 1e-6:
                c.distribuidas.append((f, nombre, "X", sentido * qh * p.g_rafaga * p.cp_barlovento_muro * b))
            elif x_max is not None and abs(x - (x_max if sentido > 0 else x_min)) < 1e-6:
                c.distribuidas.append((f, nombre, "X", -sentido * qh * p.g_rafaga * p.cp_sotavento_muro * b))
        # techo: presión normal a cada faldón (positiva hacia la superficie)
        for f in techo:
            x0, z0, x1, z1 = geo(f)
            if x1 < x0:
                x0, z0, x1, z1 = x1, z1, x0, z0
            L = math.hypot(x1 - x0, z1 - z0)
            nx, nz = -(z1 - z0) / L, (x1 - x0) / L                     # normal hacia arriba (afuera)
            barlovento = nx * sentido < -1e-6 or (abs(nx) < 1e-6)     # faldón que mira al viento (o techo plano)
            if barlovento:
                succion, presion = _interp(CP_TECHO_BARLOVENTO, ang, 1), _interp(CP_TECHO_BARLOVENTO, ang, 2)
                cp = succion if gcpi > 0 else presion                 # con +GCpi, la succión externa es la crítica
            else:
                cp = _interp(CP_TECHO_SOTAVENTO, ang, 1)
            pn = qh * (p.g_rafaga * cp - gcpi) * b                    # + hacia la superficie
            c.distribuidas.append((f, nombre, "X", -pn * nx))
            c.distribuidas.append((f, nombre, "Z", -pn * nz))

    # NCh3171 §9.1.1 (resistencia); E es el sismo del espectro (en las dos direcciones)
    c.combos["C1_1.4D"] = {"DEAD": 1.4}
    c.combos["C2_1.2D+0.5Lr"] = {"DEAD": 1.2, "LR": 0.5}
    for w in ("W1", "W2", "W3", "W4"):
        c.combos[f"C3b_1.2D+1.6Lr+0.8{w}"] = {"DEAD": 1.2, "LR": 1.6, w: 0.8}
        c.combos[f"C4_1.2D+1.6{w}+0.5Lr"] = {"DEAD": 1.2, w: 1.6, "LR": 0.5}
        c.combos[f"C6_0.9D+1.6{w}"] = {"DEAD": 0.9, w: 1.6}
    c.combos["C5_1.2D+1.4E"] = {"DEAD": 1.2, "E": 1.4}
    c.combos["C7_0.9D+1.4E"] = {"DEAD": 0.9, "E": 1.4}
    return c
