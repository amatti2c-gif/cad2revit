"""Cargas y combinaciones del modelo 2D de un eje (tonf, m), según las normas chilenas:

- Peso propio y peso del techo (D).
- Sobrecarga de techo Lr, NCh1537:2009 §5.3.2 y §8.2 (techo accesible solo para mantención, Lo = 1 kPa reducida por
  área tributaria R1 y pendiente R2, mínimo 0,3 kPa).
- Viento W, NCh432:2010 método analítico para el SPRFV, con el viento en el plano del eje: q = 0,613·Kz·Kzt·Kd·V²·I
  [N/m²] (ec. 21), p = q·(G·Cp − GCpi). GCpi ±0,18 (Tabla 13, cerrado); muros Tabla 14 (barlovento 0,8 con q_z,
  sotavento según L/B); techo Tabla 15 (θ y h/L, los dos valores de Cp; θ < 10° por zonas). Ocho casos: ±X × valor
  de Cp × ±GCpi.
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
# NCh432:2010 Tabla 15 — Cp de techos (usar con q_h), viento normal al borde, θ ≥ 10°:
# barlovento: h/L → [(θ, valor 1, valor 2)] (dos valores: se diseña para ambos, nota 3); θ ≥ 60°: 0,01·θ
CP_TECHO_BARLOVENTO = {
    0.25: [(10, -0.70, -0.18), (15, -0.5, 0.0), (20, -0.30, 0.20), (25, -0.20, 0.30), (30, -0.20, 0.30),
           (35, 0.0, 0.40), (45, 0.0, 0.40), (60, 0.60, 0.60)],
    0.5: [(10, -0.90, -0.18), (15, -0.7, -0.18), (20, -0.40, 0.0), (25, -0.30, 0.20), (30, -0.20, 0.20),
          (35, -0.20, 0.30), (45, 0.0, 0.40), (60, 0.60, 0.60)],
    1.0: [(10, -1.30, -0.18), (15, -1.0, -0.18), (20, -0.7, -0.18), (25, -0.50, 0.0), (30, -0.30, 0.20),
          (35, -0.20, 0.20), (45, 0.0, 0.30), (60, 0.60, 0.60)],
}
# sotavento: h/L → [(θ, Cp)]
CP_TECHO_SOTAVENTO = {
    0.25: [(10, -0.30), (15, -0.50), (20, -0.60), (90, -0.60)],
    0.5: [(10, -0.50), (15, -0.50), (20, -0.60), (90, -0.60)],
    1.0: [(10, -0.70), (15, -0.60), (20, -0.60), (90, -0.60)],
}
# θ < 10° (y paralelo al borde): por distancia horizontal desde el borde de barlovento, en múltiplos de h;
# segundo valor −0,18 en todas las zonas
CP_TECHO_PLANO = {
    0.5: [(0.5, -0.9), (1.0, -0.9), (2.0, -0.5), (1e9, -0.3)],
    1.0: [(0.5, -1.3), (1e9, -0.7)],
}
# NCh432 Tabla 14 — muro a sotavento según L/B
CP_MURO_SOTAVENTO = [(1.0, -0.5), (2.0, -0.3), (4.0, -0.2)]


@dataclass
class ParametrosCargas:
    ancho_colaborante: float | None = None   # m: pasa las cargas de superficie (tonf/m²) a líneas (tonf/m); None = el
                                             # ancho tributario del eje que exportó Revit
    peso_techo: float = 0.03            # tonf/m² de techo (cubierta, costaneras, aislación)
    lo_techo: float = 0.10              # tonf/m², NCh1537 Tabla 4: techo con acceso solo para mantención (1 kPa)
    reducir_lr: bool = True             # NCh1537 §8.2: reducción por área tributaria (R1) y pendiente (R2)
    # viento
    v_viento: float = 40.0              # m/s, velocidad básica (NCh432 Tabla 6: 35°–42° S; Ancud, 41°52' S)
    exposicion: str = "C"
    kzt: float = 1.0
    kd: float = 0.85
    importancia_viento: float = 1.0
    g_rafaga: float = 0.85
    gcpi: float = 0.18                  # edificio cerrado
    cp_barlovento_muro: float = 0.8     # NCh432 Tabla 14 (con q_z)
    ancho_edificio: float | None = None # B: dimensión del edificio normal al viento (m), para Cp del muro a
                                        # sotavento según L/B; None = L/B ≤ 1 (Cp −0,5)
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


def _lin(pares, x):
    """Interpolación lineal en una lista [(x, y)] ordenada; fuera del rango, el extremo."""
    if x <= pares[0][0]:
        return pares[0][1]
    for (xa, ya), (xb, yb) in zip(pares, pares[1:]):
        if xa <= x <= xb:
            return ya + (yb - ya) * (x - xa) / (xb - xa)
    return pares[-1][1]


def _por_hl(tabla: dict, h_l: float, f):
    """Valor de una tabla por h/L (filas 0,25 / 0,5 / 1,0): se interpola linealmente entre filas."""
    filas = sorted(tabla)
    return _lin([(k, f(tabla[k])) for k in filas], h_l)


def cp_techo_barlovento(theta: float, h_l: float) -> tuple[float, float]:
    """NCh432 Tabla 15, barlovento (θ ≥ 10°): los dos valores de Cp."""
    if theta >= 60:
        return 0.01 * theta, 0.01 * theta
    return tuple(_por_hl(CP_TECHO_BARLOVENTO, h_l, lambda fila, c=c: _lin([(t[0], t[c]) for t in fila], theta))
                 for c in (1, 2))


def cp_techo_sotavento(theta: float, h_l: float) -> float:
    return _por_hl(CP_TECHO_SOTAVENTO, h_l, lambda fila: _lin(fila, theta))


def cp_techo_plano(dist: float, h: float, h_l: float) -> float:
    """NCh432 Tabla 15, θ < 10°: Cp (valor 1) a una distancia dist del borde de barlovento."""
    def zona(fila):
        for lim, cp in fila:
            if dist <= lim * h:
                return cp
        return fila[-1][1]
    return _lin([(0.5, zona(CP_TECHO_PLANO[0.5])), (1.0, zona(CP_TECHO_PLANO[1.0]))], h_l)


def cp_muro_sotavento(l_b: float) -> float:
    return _lin(CP_MURO_SOTAVENTO, l_b)


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
    ancho: float = 1.0                                 # ancho colaborante usado (m)
    resumen: list = field(default_factory=list)


def cargas_eje(mod, p: ParametrosCargas | None = None) -> CargasEje:
    """Cargas sobre las barras del modelo (mod: eje_revit.ModeloEje) y combinaciones NCh3171."""
    p = p or ParametrosCargas()
    c = CargasEje()
    if p.ancho_colaborante:
        b = float(p.ancho_colaborante)
        origen_b = "dado en la app" + (f"; el ancho tributario de Revit es {mod.ancho_tributario:.2f} m" if mod.ancho_tributario else "")
    elif mod.ancho_tributario:
        b, origen_b = mod.ancho_tributario, "ancho tributario del eje en Revit (mitad de la distancia a los ejes vecinos)"
    else:
        b, origen_b = 1.0, "sin ancho tributario en el archivo: se usó 1 m"
    c.ancho = b
    c.resumen.append(f"Ancho colaborante b = {b:.2f} m ({origen_b}): carga por m² × b = carga por metro lineal")
    c.resumen.append(f"D techo: {p.peso_techo * 1000:.0f} kgf/m² × {b:.2f} m = {p.peso_techo * b * 1000:.0f} kgf/m sobre las vigas de techo")
    c.patrones = {"DEAD": "DEAD", "LR": "ROOF LIVE", "E": "QUAKE"}

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
        c.resumen.append(f"Lr = {lr * 1000:.0f} kgf/m² × {b:.2f} m = {lr * b * 1000:.0f} kgf/m (proyección horizontal) — NCh1537 §8.2: Lo {p.lo_techo * 1000:.0f} kgf/m², "
                         f"A_T {at:.1f} m² → R1 = {red['R1']:.3f}; pendiente F {pend * 100:.1f} % → R2 = {red['R2']:.3f}; "
                         f"R1·R2 = {red['R1R2']:.3f} → factor {red['factor']:.3f}" + (" (mínimo 0,3 kPa)" if lr > p.lo_techo * red['factor'] + 1e-9 else ""))
    else:
        c.resumen.append(f"Lr = {lr * 1000:.0f} kgf/m² × {b:.2f} m = {lr * b * 1000:.0f} kgf/m — sin reducción (Lo); con NCh1537 §8.2 sería factor "
                         f"{red['factor']:.3f} (R1 {red['R1']:.3f}, R2 {red['R2']:.3f})")

    # W: viento en el plano del eje (NCh432 método analítico), en +X y −X, con los dos valores de Cp del techo
    # (nota 3 de la Tabla 15) y las dos presiones internas ±GCpi (Tabla 13): 8 casos
    qh = q_viento(p, h)
    xs_mod = [x for x, _ in mod.nudos.values()]
    largo = max(xs_mod) - min(xs_mod) or 1.0                  # L: dimensión paralela al viento (a lo largo del eje)
    h_l = h / largo
    l_b = largo / p.ancho_edificio if p.ancho_edificio else 1.0
    cp_sot_muro = cp_muro_sotavento(l_b)
    plano = ang < 10
    h_alero = max(z_alero - z_base, 0.1)
    c.resumen.append(f"Viento NCh432: q_h = {qh * 1000:.1f} kgf/m² (V {p.v_viento} m/s, exposición {p.exposicion}, h {h:.2f} m), "
                     f"L = {largo:.2f} m, h/L = {h_l:.2f}, θ = {ang:.1f}°; p = q·(G·Cp − GCpi) × {b:.2f} m")
    if plano:
        c.resumen.append(f"  techo con θ < 10°: Cp por zonas desde el borde de barlovento (0–h/2, h/2–h, h–2h, > 2h; "
                         f"h = {h_alero:.2f} m) y −0,18")
    else:
        b1, b2 = cp_techo_barlovento(ang, h_l)
        c.resumen.append(f"  techo (Tabla 15): barlovento Cp = {b1:.2f} o {b2:.2f}; sotavento Cp = {cp_techo_sotavento(ang, h_l):.2f}")
    c.resumen.append(f"  muros (Tabla 14): barlovento Cp = {p.cp_barlovento_muro:.1f} con q_z; sotavento Cp = {cp_sot_muro:.2f} "
                     f"(L/B = {l_b:.2f}" + ("" if p.ancho_edificio else ", sin B: se supuso ≤ 1") + ")")
    x_min = min((geo(f)[0] for f in pilares), default=None)
    x_max = max((geo(f)[0] for f in pilares), default=None)
    casos = []
    for sentido in (1, -1):
        for valor in (1, 2):
            for gcpi in (p.gcpi, -p.gcpi):
                casos.append((f"W{len(casos) + 1}", sentido, valor, gcpi))
    c.patrones.update({n: "WIND" for n, *_ in casos})
    for nombre, sentido, valor, gcpi in casos:
        c.resumen.append(f"  {nombre}: viento {'+X' if sentido > 0 else '−X'}, Cp techo valor {valor}, GCpi {gcpi:+.2f}")
        # muros: barlovento con q_z a la altura de cada tramo de pilar; sotavento con q_h (la interna se anula)
        for f in pilares:
            x, za_, _, zb_ = geo(f)
            if x_min is not None and abs(x - (x_min if sentido > 0 else x_max)) < 1e-6:
                qz = q_viento(p, (za_ + zb_) / 2 - z_base)
                c.distribuidas.append((f, nombre, "X", sentido * qz * p.g_rafaga * p.cp_barlovento_muro * b))
            elif x_max is not None and abs(x - (x_max if sentido > 0 else x_min)) < 1e-6:
                c.distribuidas.append((f, nombre, "X", -sentido * qh * p.g_rafaga * cp_sot_muro * b))
        # techo: presión normal a cada faldón (positiva hacia la superficie)
        borde = x_min if sentido > 0 else x_max
        if borde is None:
            borde = min(xs_mod) if sentido > 0 else max(xs_mod)
        for f in techo:
            x0, z0, x1, z1 = geo(f)
            if x1 < x0:
                x0, z0, x1, z1 = x1, z1, x0, z0
            L = math.hypot(x1 - x0, z1 - z0)
            nx, nz = -(z1 - z0) / L, (x1 - x0) / L                     # normal hacia arriba (afuera)
            if plano:
                cp = cp_techo_plano(abs((x0 + x1) / 2 - borde), h_alero, h_l) if valor == 1 else -0.18
            elif nx * sentido < -1e-6:                                 # faldón que mira al viento
                cp = cp_techo_barlovento(ang, h_l)[valor - 1]
            else:
                cp = cp_techo_sotavento(ang, h_l)
            pn = qh * (p.g_rafaga * cp - gcpi) * b                    # + hacia la superficie
            c.distribuidas.append((f, nombre, "X", -pn * nx))
            c.distribuidas.append((f, nombre, "Z", -pn * nz))

    # NCh3171 §9.1.1 (resistencia); E es el sismo del espectro (en las dos direcciones)
    c.combos["C1_1.4D"] = {"DEAD": 1.4}
    c.combos["C2_1.2D+0.5Lr"] = {"DEAD": 1.2, "LR": 0.5}
    for w in [n for n, t in c.patrones.items() if t == "WIND"]:
        c.combos[f"C3b_1.2D+1.6Lr+0.8{w}"] = {"DEAD": 1.2, "LR": 1.6, w: 0.8}
        c.combos[f"C4_1.2D+1.6{w}+0.5Lr"] = {"DEAD": 1.2, w: 1.6, "LR": 0.5}
        c.combos[f"C6_0.9D+1.6{w}"] = {"DEAD": 0.9, w: 1.6}
    c.combos["C5_1.2D+1.4E"] = {"DEAD": 1.2, "E": 1.4}
    c.combos["C7_0.9D+1.4E"] = {"DEAD": 0.9, "E": 1.4}
    return c
