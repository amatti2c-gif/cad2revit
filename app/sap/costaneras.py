"""Costaneras de techo (elementos secundarios): perfiles de acero conformado en frío según AISI S100-16 (método de
los anchos efectivos), que adopta la NCh427/2, con las cargas de la NCh1537, NCh432 y las combinaciones NCh3171.

Datos (archivo .costaneras.json del botón «Costaneras a análisis» de Revit): las costaneras de cada sistema de vigas
(Structural Framing System), con su perfil, y las vigas que las soportan por debajo. De ahí salen:

- apoyos de cada costanera: los cruces en planta con las vigas que pasan por debajo (vigas cuya línea queda a menos
  de `tol_apoyo` bajo la costanera) → luces libres y voladizos; cada elemento de Revit es una viga continua sobre sus
  apoyos (si en Revit cada tramo es un elemento, cada uno queda simplemente apoyado);
- ancho colaborante: la mitad de la separación a las costaneras vecinas a cada lado (medida en el plano del techo);
  en las de borde, hasta el borde del sistema;
- pendiente del techo: la del plano de las costaneras (o la de las vigas de apoyo, si hay una sola).

Cargas por metro de costanera (ancho colaborante s, pendiente θ):
- D: cubierta (kgf/m² de techo) × s + peso propio del perfil;
- Lr: NCh1537 §5.3.2 y §8.2 (Lo = 1 kPa reducido por R1 con el área tributaria luz × s y R2 con la pendiente,
  mínimo 0,3 kPa), por m² de proyección horizontal: × s·cos θ;
- W: NCh432:2010 componentes y revestimientos (techos h ≤ 18 m, como ASCE 7-05 Fig. 6-11B/C/D): p = q_h·(GCp − GCpi),
  GCp según la zona (1 interior, 2 bordes, 3 esquinas; ancho a = mín(0,1·menor dimensión, 0,4·h) ≥ máx(0,04·menor
  dimensión; 0,9 m)) y el área efectiva A = luz × máx(s, luz/3); presión neta mínima 0,48 kPa;
- nieve S opcional (por m² de proyección horizontal).
Las cargas gravitacionales se descomponen en la normal al techo (eje fuerte) y la paralela (eje débil); el viento es
normal al techo. En el eje débil, los tensores (a L/2 o L/3) son apoyos; también arriostran el volcamiento.

Verificaciones (LRFD, AISI S100-16): flexión con anchos efectivos (F2.2, Apéndice 1: almas con gradiente, alas con
atiesador de borde 1.3), volcamiento lateral-torsional entre tensores (F2.1.1 perfiles C, F2.1.2 perfiles Z, con Cb del
diagrama), flexión biaxial (H1.2), corte (G2.1) y flexión con corte (H2); deformación normal al techo en servicio.
No se verifican: pandeo distorsional (F4), aplastamiento del alma en los apoyos (G5) ni la conexión a las vigas.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np

from app.sap.eje_cargas import ParametrosCargas, q_viento, reduccion_lr

G_ACC = 9.80665
E_AISI = 203_000.0           # MPa (29 500 ksi)
G_AISI = 78_000.0            # MPa (11 300 ksi)
RHO_ACERO = 7850.0           # kg/m³
KGF_M2 = G_ACC * 1e-6        # 1 kgf/m² en N/mm²
PHI_B, PHI_V = 0.90, 0.95


# ---------------------------------------------------------------------------------------------- perfil
@dataclass
class Perfil:
    """Perfil conformado en frío, en mm: «CA» canal atiesado (C con labios), «C» canal, «ZA» Z atiesada, «Z», «TUB»
    tubo rectangular (cajón). h altura total, b ancho del ala, c labio (total), t espesor."""
    tipo: str
    h: float
    b: float
    t: float
    c: float = 0.0
    r: float | None = None       # radio interior de doblado; None = t
    texto: str = ""

    def nombre(self) -> str:
        n = lambda v: f"{v:g}"
        if self.tipo in ("CA", "ZA"):
            return f"{self.tipo} {n(self.h)}x{n(self.b)}x{n(self.c)}x{n(self.t)}"
        return f"{self.tipo} {n(self.h)}x{n(self.b)}x{n(self.t)}"


def leer_perfil(texto: str, parametros: dict | None = None) -> Perfil | None:
    """Perfil desde un nombre como «CA 150x50x15x2», «C 100x50x3», «Z 200x70x20x2,5» o «Tubular 100x50x2» (mm). Si el
    nombre no sirve, con los parámetros del tipo de Revit (mm): altura (h/d/Altura), ancho (b/bf/Ancho), labio (c/D/
    Labio) y espesor (t/e/Espesor)."""
    txt = (texto or "").strip()
    nums = [float(x.replace(",", ".")) for x in re.findall(r"\d+(?:[.,]\d+)?", txt)]
    low = txt.lower()
    tub = any(k in low for k in ("tub", "□", "rect", "box", "cajon", "cajón", "hss", "cuadr"))
    zeta = bool(re.search(r"(^|[^a-z])z", low)) and not tub
    if len(nums) >= 3:
        if tub:
            return Perfil("TUB", nums[0], nums[1], nums[-1], texto=txt)
        if len(nums) >= 4:
            return Perfil("ZA" if zeta else "CA", nums[0], nums[1], nums[3], c=nums[2], texto=txt)
        return Perfil("Z" if zeta else "C", nums[0], nums[1], nums[2], texto=txt)
    p = {str(k).lower(): v for k, v in (parametros or {}).items() if isinstance(v, (int, float)) and v > 0}

    def buscar(*claves):
        for k in claves:
            if k in p:
                return float(p[k])
        return None
    h = buscar("h", "d", "altura", "alto", "height", "depth")
    b = buscar("b", "bf", "ancho", "ala", "width", "flange width")
    t = buscar("t", "e", "espesor", "thickness", "tw", "tf")
    c = buscar("c", "labio", "lip", "d1", "dl")
    if h and b and t:
        return Perfil(("TUB" if tub else ("ZA" if zeta else "CA")) if c and not tub else ("TUB" if tub else ("Z" if zeta else "C")),
                      h, b, t, c=c or 0.0, texto=txt)
    return None


@dataclass
class _Placa:
    p1: tuple            # extremo apoyado (esquina con otro elemento) — coordenadas de línea media (x, y), mm
    p2: tuple
    t: float
    tipo: str            # "rig" (rigidizado), "libre" (no rigidizado: p2 libre), "labio_ala" (ala con atiesador de borde)
    o1: float            # distancia desde p1 al inicio del tramo plano
    w: float             # ancho plano
    labio: int | None = None

    @property
    def L(self):
        return math.hypot(self.p2[0] - self.p1[0], self.p2[1] - self.p1[1])

    def punto(self, s):
        L = self.L or 1.0
        return (self.p1[0] + (self.p2[0] - self.p1[0]) * s / L, self.p1[1] + (self.p2[1] - self.p1[1]) * s / L)


def _placas(pf: Perfil) -> list[_Placa]:
    """Modelo de línea media con esquinas rectas: x horizontal (sentido de las alas), y vertical (alma)."""
    t = pf.t
    r = pf.r if pf.r is not None else t
    hc, bc = pf.h - t, pf.b - t
    cc = max(pf.c - t / 2, 0.0)
    e = r + t / 2                                    # de la línea media de la esquina al inicio del tramo plano
    w_alma = max(pf.h - 2 * (r + t), 0.0)
    if pf.tipo == "TUB":
        w_ala = max(pf.b - 2 * (r + t), 0.0)
        x0, x1, y0, y1 = -bc / 2, bc / 2, -hc / 2, hc / 2
        return [_Placa((x0, y0), (x0, y1), t, "rig", e, w_alma), _Placa((x1, y0), (x1, y1), t, "rig", e, w_alma),
                _Placa((x0, y1), (x1, y1), t, "rig", e, w_ala), _Placa((x0, y0), (x1, y0), t, "rig", e, w_ala)]
    labio = pf.tipo in ("CA", "ZA") and cc > 0
    w_ala = max(pf.b - (2 if labio else 1) * (r + t), 0.0)
    w_lab = max(pf.c - (r + t), 0.0)
    sx_inf = 1 if pf.tipo in ("C", "CA") else -1     # Z: el ala inferior va hacia el otro lado
    placas = [_Placa((0, -hc / 2), (0, hc / 2), t, "rig", e, w_alma)]
    for sy, sx in ((1, 1), (-1, sx_inf)):
        y = sy * hc / 2
        placas.append(_Placa((0, y), (sx * bc, y), t, "labio_ala" if labio else "libre", e, w_ala))
        if labio:
            placas[-1].labio = len(placas)
            placas.append(_Placa((sx * bc, y), (sx * bc, y - sy * cc), t, "libre", e, w_lab))
    return placas


def _rho(w, t, f, k):
    if w <= 0 or f <= 0:
        return 1.0
    lam = 1.052 / math.sqrt(k) * (w / t) * math.sqrt(f / E_AISI)
    return 1.0 if lam <= 0.673 else min(1.0, (1 - 0.22 / lam) / lam)


def _propiedades(segs):
    """segs: [(x1, y1, x2, y2, t)] → A, xc, yc, Ixx, Iyy (centroidales, de línea media)."""
    A = sx = sy = 0.0
    for x1, y1, x2, y2, t in segs:
        a = math.hypot(x2 - x1, y2 - y1) * t
        A += a
        sx += a * (x1 + x2) / 2
        sy += a * (y1 + y2) / 2
    if A <= 0:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    xc, yc = sx / A, sy / A
    Ixx = Iyy = 0.0
    for x1, y1, x2, y2, t in segs:
        a = math.hypot(x2 - x1, y2 - y1) * t
        ym, xm = (y1 + y2) / 2, (x1 + x2) / 2
        Ixx += a * ((y2 - y1) ** 2 / 12 + (ym - yc) ** 2)
        Iyy += a * ((x2 - x1) ** 2 / 12 + (xm - xc) ** 2)
    return A, xc, yc, Ixx, Iyy


def _ineficaces(pl: _Placa, f1: float, f2: float, extra: dict) -> list[tuple[float, float]]:
    """Tramos no efectivos (distancias desde p1 a lo largo de la línea media) de una placa con tensiones f1 en el
    inicio del tramo plano (lado p1) y f2 en su fin (lado p2); compresión positiva."""
    w, t = pl.w, pl.t
    fmax = max(f1, f2)
    if fmax <= 0 or w <= 0:
        return []
    a0 = pl.o1
    if pl.tipo == "rig":
        alto_en_p1 = f1 >= f2
        fa, fb = (f1, f2) if alto_en_p1 else (f2, f1)            # fa: extremo más comprimido
        psi = fb / fa
        if psi >= 0:
            k = 4 + 2 * (1 - psi) ** 3 + 2 * (1 - psi)
            be = _rho(w, t, fa, k) * w
            b1 = be / (3 - psi)
            b2 = be - b1
            if b1 + b2 >= w - 1e-9:
                return []
            ia, ib = b1, w - b2                                  # desde el extremo más comprimido
        else:
            pa = -psi
            k = 4 + 2 * (1 + pa) ** 3 + 2 * (1 + pa)
            be = _rho(w, t, fa, k) * w
            wc = w * fa / (fa - fb)                              # ancho comprimido
            b1 = be / (3 + pa)
            b2 = be / 2 if pa > 0.236 else be - b1
            if b1 + b2 >= wc - 1e-9:
                return []
            ia, ib = b1, wc - b2
        if not alto_en_p1:
            ia, ib = w - ib, w - ia
        return [(a0 + ia, a0 + ib)]
    if pl.tipo == "libre":
        # no rigidizado (labio o ala sin labio): compresión uniforme con la mayor tensión (conservador), k = 0,43;
        # la parte efectiva es la vecina al apoyo; si la punta está traccionada, solo la zona comprimida
        rho = _rho(w, t, fmax, 0.43)
        rho *= extra.get("RI", 1.0)                              # labio de un ala con atiesador: ds = ds'·RI
        be = rho * w
        if f2 <= 0 < f1:                                         # punta traccionada: se pierde la parte comprimida que exceda be
            wc = w * f1 / (f1 - f2)
            return [] if be >= wc else [(a0 + be, a0 + wc)]
        if be >= w - 1e-9:
            return []
        return [(a0 + be, a0 + w)]
    # ala con atiesador de borde (AISI S100-16 Apéndice 1, 1.3), con la mayor compresión del ala
    f = fmax
    S = 1.28 * math.sqrt(E_AISI / f)
    wt = w / t
    d_lab = extra["d_labio"]
    D_tot = extra["D_labio"]
    Is = d_lab ** 3 * t / 12
    if wt <= 0.328 * S:
        extra["RI"] = 1.0
        return []
    Ia = min(399 * t ** 4 * (wt / S - 0.328) ** 3, t ** 4 * (115 * wt / S + 5))
    n = max(0.582 - wt / (4 * S), 1 / 3)
    RI = min(Is / Ia, 1.0) if Ia > 0 else 1.0
    extra["RI"] = RI
    Dw = min(D_tot / w, 0.8)
    k = min(3.57 * RI ** n + 0.43, 4.0) if Dw <= 0.25 else min((4.82 - 5 * Dw) * RI ** n + 0.43, 4.0)
    be = _rho(w, t, f, k) * w
    b1 = be / 2 * RI                                             # junto al labio (p2)
    b2 = be - b1                                                 # junto al alma (p1)
    if be >= w - 1e-9:
        return []
    return [(a0 + b2, a0 + w - b1)]


def seccion_efectiva(pf: Perfil, eje: str, signo: int, f_obj: float, fy: float) -> dict:
    """Sección efectiva para flexión en torno a `eje` ("x": eje fuerte, alma vertical; "y": eje débil) con la fibra
    comprimida extrema en el lado `signo` (+1: y o x positivos) a la tensión f_obj (MPa), sin que la fibra traccionada
    pase de fy. Devuelve Ie, Se (a la fibra comprimida), fc y M = Se·fc (N·mm)."""
    placas = _placas(pf)
    u = (lambda p: signo * p[1]) if eje == "x" else (lambda p: signo * p[0])
    uc = max(max(u(pl.p1), u(pl.p2)) for pl in placas) + pf.t / 2
    ut = min(min(u(pl.p1), u(pl.p2)) for pl in placas) - pf.t / 2
    todos = [(*pl.p1, *pl.p2, pl.t) for pl in placas]
    A, xc, yc, Ixx, Iyy = _propiedades(todos)
    na = signo * (yc if eje == "x" else xc)
    Ie = Ixx if eje == "x" else Iyy
    fc = f_obj
    for _ in range(60):
        cc_, ct_ = uc - na, na - ut
        fc = min(f_obj, fy * cc_ / ct_) if ct_ > 0 else f_obj
        sig = lambda p: fc * (u(p) - na) / cc_
        segs = []
        extra_por_placa: dict[int, dict] = {}
        # primero las alas con atiesador (definen RI de su labio)
        orden = sorted(range(len(placas)), key=lambda i: 0 if placas[i].tipo == "labio_ala" else 1)
        quitar: dict[int, list] = {}
        for i in orden:
            pl = placas[i]
            ex = extra_por_placa.setdefault(i, {})
            if pl.tipo == "labio_ala":
                lab = placas[pl.labio]
                ex["d_labio"], ex["D_labio"] = lab.w, pf.c
            f1, f2 = sig(pl.punto(pl.o1)), sig(pl.punto(pl.o1 + pl.w))
            quitar[i] = _ineficaces(pl, f1, f2, ex)
            if pl.tipo == "labio_ala" and pl.labio is not None:
                extra_por_placa.setdefault(pl.labio, {})["RI"] = ex.get("RI", 1.0)
        for i, pl in enumerate(placas):
            cortes = [0.0]
            for a, b in quitar.get(i, []):
                cortes += [a, b]
            cortes.append(pl.L)
            for a, b in zip(cortes[::2], cortes[1::2]):
                if b - a > 1e-9:
                    pa, pb = pl.punto(a), pl.punto(b)
                    segs.append((*pa, *pb, pl.t))
        A_e, xc_e, yc_e, Ixx_e, Iyy_e = _propiedades(segs)
        na_n = signo * (yc_e if eje == "x" else xc_e)
        Ie = Ixx_e if eje == "x" else Iyy_e
        if abs(na_n - na) < 1e-4:
            na = na_n
            break
        na = na_n
    cc_ = uc - na
    Se = Ie / cc_
    return {"Ie": Ie, "Se": Se, "fc": fc, "M": Se * fc, "Ae": A_e, "na": na}


def propiedades_brutas(pf: Perfil) -> dict:
    placas = _placas(pf)
    A, xc, yc, Ixx, Iyy = _propiedades([(*pl.p1, *pl.p2, pl.t) for pl in placas])
    t = pf.t
    J = sum(pl.L * t ** 3 / 3 for pl in placas)
    hc = pf.h - t
    xs = [x for pl in placas for x in (pl.p1[0], pl.p2[0])]
    ys = [y for pl in placas for y in (pl.p1[1], pl.p2[1])]
    out = {"A": A, "Ix": Ixx, "Iy": Iyy, "J": J, "xc": xc, "yc": yc, "Sx": Ixx / (max(ys) - yc + t / 2),
           "Sy_pos": Iyy / (max(xs) - xc + t / 2), "Sy_neg": Iyy / (xc - min(xs) + t / 2),
           "rx": math.sqrt(Ixx / A), "ry": math.sqrt(Iyy / A), "Cw": 0.0, "x0": 0.0}
    if pf.tipo in ("C", "CA"):
        a, b = hc, pf.b - t
        c = max(pf.c - t / 2, 0.0) if pf.tipo == "CA" else 0.0
        # AISI Cold-Formed Steel Design Manual (esquinas rectas): centro de corte m desde la línea media del alma y Cw
        m = b * t / (12 * Ixx) * (6 * c * a ** 2 + 3 * b * a ** 2 - 8 * c ** 3)
        num = 2 * a ** 3 * b + 3 * a ** 2 * b ** 2 + 48 * c ** 4 + 112 * b * c ** 3 + 8 * a * c ** 3 + 48 * a * b * c ** 2 \
            + 12 * a ** 2 * c ** 2 + 12 * a ** 2 * b * c + 6 * a ** 3 * c
        den = 6 * a ** 2 * b + (a + 2 * c) ** 3 - 24 * a * c ** 2
        out["Cw"] = a ** 2 * b ** 2 * t / 12 * num / den
        out["m"] = m
        out["x0"] = xc + m                     # distancia del centroide al centro de corte (lados opuestos del alma)
    out["peso_kgf_m"] = A * 1e-6 * RHO_ACERO   # kg/m
    out["w_alma"] = max(pf.h - 2 * ((pf.r if pf.r is not None else t) + t), 0.0)
    out["n_almas"] = 2 if pf.tipo == "TUB" else 1
    return out


def fn_volcamiento(pf: Perfil, pr: dict, Lb: float, Cb: float, fy: float) -> dict:
    """AISI S100-16 F2.1: tensión de volcamiento lateral-torsional Fn (MPa) con largo no arriostrado Lb (mm)."""
    if pf.tipo == "TUB" or Lb <= 0:
        return {"Fe": None, "Fn": fy}
    Sf = pr["Sx"]
    if pf.tipo in ("C", "CA"):
        A = pr["A"]
        r0 = math.sqrt(pr["rx"] ** 2 + pr["ry"] ** 2 + pr["x0"] ** 2)
        sey = math.pi ** 2 * E_AISI / (Lb / pr["ry"]) ** 2
        st = (G_AISI * pr["J"] + math.pi ** 2 * E_AISI * pr["Cw"] / Lb ** 2) / (A * r0 ** 2)
        Fe = Cb * r0 * A * math.sqrt(sey * st) / Sf
    else:                                                      # Z: F2.1.2, Iyc = Iy/2
        Fe = Cb * math.pi ** 2 * E_AISI * pf.h * (pr["Iy"] / 2) / (2 * Sf * Lb ** 2)
    if Fe >= 2.78 * fy:
        Fn = fy
    elif Fe > 0.56 * fy:
        Fn = 10 / 9 * fy * (1 - 10 * fy / (36 * Fe))
    else:
        Fn = Fe
    return {"Fe": Fe, "Fn": min(Fn, fy)}


def corte_nominal(pf: Perfil, pr: dict, fy: float) -> float:
    """AISI S100-16 G2.1 (alma sin atiesadores, kv = 5,34): Vn (N)."""
    h, t = pr["w_alma"], pf.t
    if h <= 0:
        return 0.0
    kv = 5.34
    ht = h / t
    lim = math.sqrt(E_AISI * kv / fy)
    if ht <= lim:
        fv = 0.6 * fy
    elif ht <= 1.51 * lim:
        fv = 0.6 * math.sqrt(E_AISI * kv * fy) / ht
    else:
        fv = 0.904 * E_AISI * kv / ht ** 2
    return pr["n_almas"] * h * t * fv


# ---------------------------------------------------------------------------------------------- viga continua
def viga_continua(xs: np.ndarray, apoyos: set[int], q: np.ndarray, EI: float):
    """Viga sobre nudos xs (mm) con apoyos simples en los índices `apoyos` y carga uniforme q por elemento (N/mm,
    positiva hacia arriba). Devuelve (M en nudos, V en nudos (máx. abs. a cada lado), flecha v en nudos, reacciones)."""
    n = len(xs)
    K = np.zeros((2 * n, 2 * n))
    F = np.zeros(2 * n)
    for e in range(n - 1):
        L = xs[e + 1] - xs[e]
        k = EI / L ** 3 * np.array([[12, 6 * L, -12, 6 * L], [6 * L, 4 * L * L, -6 * L, 2 * L * L],
                                    [-12, -6 * L, 12, -6 * L], [6 * L, 2 * L * L, -6 * L, 4 * L * L]])
        idx = [2 * e, 2 * e + 1, 2 * e + 2, 2 * e + 3]
        K[np.ix_(idx, idx)] += k
        p = q[e]
        F[idx] += np.array([p * L / 2, p * L * L / 12, p * L / 2, -p * L * L / 12])
    libres = [i for i in range(2 * n) if not (i % 2 == 0 and i // 2 in apoyos)]
    U = np.zeros(2 * n)
    U[libres] = np.linalg.solve(K[np.ix_(libres, libres)], F[libres])
    R = K @ U - F
    M = np.zeros(n)
    V = np.zeros(n)
    for e in range(n - 1):
        L = xs[e + 1] - xs[e]
        k = EI / L ** 3 * np.array([[12, 6 * L, -12, 6 * L], [6 * L, 4 * L * L, -6 * L, 2 * L * L],
                                    [-12, -6 * L, 12, -6 * L], [6 * L, 2 * L * L, -6 * L, 4 * L * L]])
        p = q[e]
        f = k @ U[2 * e:2 * e + 4] - np.array([p * L / 2, p * L * L / 12, p * L / 2, -p * L * L / 12])
        V1, M1 = f[0], f[1]
        M[e] = -M1                                               # flexión positiva: tracción abajo
        M[e + 1] = -M1 + V1 * L + p * L * L / 2
        for i, v in ((e, V1), (e + 1, V1 + p * L)):
            if abs(v) > abs(V[i]):
                V[i] = v
    return M, V, U[0::2], R[0::2]


def _cb(m: np.ndarray) -> float:
    """AISI S100-16 F2.1.1: Cb = 12,5·Mmáx/(2,5·Mmáx + 3·MA + 4·MB + 3·MC) en un tramo entre arriostramientos."""
    if len(m) < 3:
        return 1.0
    xs = np.linspace(0, 1, len(m))
    a = np.abs(m)
    mm = a.max()
    if mm <= 0:
        return 1.0
    MA, MB, MC = (abs(np.interp(f, xs, m)) for f in (0.25, 0.5, 0.75))
    return min(12.5 * mm / (2.5 * mm + 3 * MA + 4 * MB + 3 * MC), 3.0)


# ---------------------------------------------------------------------------------------------- viento C&C
# NCh432:2010 (= ASCE 7-05) componentes y revestimientos de techos a dos aguas, h ≤ 18 m: (GCp a 0,93 m², a 9,3 m²)
GCP = {
    "plano": {"pos": (0.3, 0.2), 1: (-1.0, -0.9), 2: (-1.8, -1.1), 3: (-2.8, -1.1)},       # θ ≤ 7°
    "medio": {"pos": (0.5, 0.3), 1: (-0.9, -0.8), 2: (-1.7, -1.2), 3: (-2.6, -2.0)},       # 7° < θ ≤ 27°
    "alto": {"pos": (0.9, 0.8), 1: (-1.0, -0.8), 2: (-1.2, -1.0), 3: (-1.2, -1.0)},        # 27° < θ ≤ 45°
}


def gcp(theta: float, zona: int | str, area: float) -> float:
    fila = GCP["plano" if theta <= 7 else ("medio" if theta <= 27 else "alto")][zona]
    a = min(max(area, 0.929), 9.29)
    f = math.log10(a / 0.929)                                    # 0 a 1 entre 10 y 100 pie²
    return fila[0] + (fila[1] - fila[0]) * f


# ---------------------------------------------------------------------------------------------- parámetros
@dataclass
class ParametrosCostaneras:
    perfiles: dict = field(default_factory=dict)  # tipo de Revit → perfil («CA 150x50x15x2»), si no se lee del nombre
    perfil_defecto: str = ""
    fy: float = 270.0                    # MPa (A270ES)
    radio_interior: float | None = None  # mm; None = t
    peso_cubierta: float = 15.0          # kgf/m² de techo: planchas, fijaciones, aislación
    lo_techo: float = 1000 / G_ACC       # kgf/m² (1 kPa), NCh1537 Tabla 4: techo solo con acceso de mantención
    reducir_lr: bool = True
    nieve: float = 0.0                   # kgf/m² de proyección horizontal
    v_viento: float = 40.0
    exposicion: str = "C"
    kzt: float = 1.0
    kd: float = 0.85
    importancia_viento: float = 1.0
    gcpi: float = 0.18
    h_techo: float | None = None         # m: altura media del techo (q_h); None = desde Revit
    zona_viento: str = "auto"            # "auto" (según la posición) o "1", "2", "3"
    ancho_colaborante: float | None = None  # m; None = según la separación de las costaneras
    pendiente: float | None = None       # °; None = la del techo en Revit
    tensores: str = "auto"               # "ninguno", "L/2", "L/3", "auto" (L/2 hasta 6 m, L/3 sobre 6 m)
    cubierta_arriostra: bool = False     # la cubierta atornillada arriostra el ala superior (flexión positiva)
    limite_gravedad: float = 200.0       # L/… con D + Lr
    limite_viento: float = 150.0         # L/… con W
    tol_apoyo: float = 1.0               # m: distancia máxima bajo la costanera a la que se busca la viga de apoyo


def _tensores_por_tramo(regla: str, luz_m: float) -> int:
    r = (regla or "auto").upper()
    if r in ("NINGUNO", "0"):
        return 0
    if r in ("L/2", "1"):
        return 1
    if r in ("L/3", "2"):
        return 2
    return 1 if luz_m <= 6.0 else 2


# ---------------------------------------------------------------------------------------------- geometría
def _v(p):
    return np.array(p, dtype=float)


def _geometria(datos: dict, par: ParametrosCostaneras) -> tuple[list[dict], dict]:
    """Apoyos, luces, ancho colaborante y pendiente de cada costanera; datos generales del techo."""
    cost = datos.get("costaneras") or []
    vigas = datos.get("vigas") or []
    sistemas = {s.get("id"): s for s in (datos.get("sistemas") or [])}
    ids_cost = {c["id"] for c in cost}
    vigas = [v for v in vigas if v.get("id") not in ids_cost]
    pts_techo = [(_v(c["p0"]), _v(c["p1"])) for c in cost] + [(_v(v["p0"]), _v(v["p1"])) for v in vigas]
    z_base = datos.get("z_base")
    zs = [p[2] for a, b in pts_techo for p in (a, b)]
    out = []
    for c in cost:
        a, b = _v(c["p0"]), _v(c["p1"])
        d = b - a
        L = float(np.linalg.norm(d))
        dh = math.hypot(d[0], d[1])
        nota = []
        if L < 0.1 or dh < 1e-6:
            continue
        u = d / L
        uh = np.array([d[0], d[1]]) / dh
        vh = np.array([-uh[1], uh[0]])
        # apoyos: cruces en planta con vigas que pasan por debajo
        apoyos = []
        for v in vigas:
            p, q_ = _v(v["p0"]), _v(v["p1"])
            e = q_ - p
            den = d[0] * e[1] - d[1] * e[0]
            if abs(den) < 1e-9 * max(dh, 1e-9) * max(math.hypot(e[0], e[1]), 1e-9):
                continue                                          # paralela
            w = p - a
            t = (w[0] * e[1] - w[1] * e[0]) / den
            s = (w[0] * d[1] - w[1] * d[0]) / den
            tol_t = 0.05 / L
            le = math.hypot(e[0], e[1])
            tol_s = 0.05 / le if le > 0 else 0
            if -tol_t <= t <= 1 + tol_t and -tol_s <= s <= 1 + tol_s:
                zc = a[2] + d[2] * t
                zv = p[2] + e[2] * s
                bajo = zc - zv
                if -0.10 <= bajo <= par.tol_apoyo:
                    apoyos.append({"t": min(max(t, 0.0), 1.0) * L, "viga": v.get("id"), "tipo": v.get("tipo"), "bajo": bajo,
                                   "dir_viga": e})
        apoyos.sort(key=lambda x: x["t"])
        unicos = []
        for ap in apoyos:                                         # vigas dobles o cruces repetidos: el más cercano por debajo
            if unicos and ap["t"] - unicos[-1]["t"] < 0.05:
                if ap["bajo"] < unicos[-1]["bajo"]:
                    unicos[-1] = ap
                continue
            unicos.append(ap)
        apoyos = unicos
        # costaneras vecinas (mismo sistema, paralelas, que se traslapan) a cada lado: separación en el plano del techo
        lados = {1: [], -1: []}
        for o in cost:
            if o is c or o.get("sistema") != c.get("sistema"):
                continue
            pa, pb = _v(o["p0"]), _v(o["p1"])
            do = pb - pa
            lo = np.linalg.norm(do)
            if lo < 0.1 or abs(np.dot(do / lo, u)) < 0.98:
                continue
            ta, tb = sorted((np.dot(pa - a, u), np.dot(pb - a, u)))
            if tb < 0.1 * L or ta > 0.9 * L:
                continue
            w = (pa + pb) / 2 - a
            wp = w - np.dot(w, u) * u
            dist = float(np.linalg.norm(wp))
            if dist < 0.02:
                continue
            lado = 1 if wp[0] * vh[0] + wp[1] * vh[1] >= 0 else -1
            lados[lado].append((dist, wp))
        vecinos = {k: min(v, key=lambda x: x[0]) if v else None for k, v in lados.items()}
        # pendiente del techo
        theta = None
        for vv in vecinos.values():
            if vv is not None:
                wp = vv[1]
                theta = math.degrees(math.atan2(abs(wp[2]), math.hypot(wp[0], wp[1])))
                break
        if theta is None and apoyos:
            e = apoyos[0]["dir_viga"]
            ep = e - np.dot(e, u) * u
            if np.linalg.norm(ep) > 1e-9:
                theta = math.degrees(math.atan2(abs(ep[2]), math.hypot(ep[0], ep[1])))
        if theta is None:
            theta = 0.0
        if par.pendiente is not None:
            theta = float(par.pendiente)
        # ancho colaborante
        sist = sistemas.get(c.get("sistema")) or {}
        contorno = [_v(p) for p in (sist.get("contorno") or [])]

        def borde(lado):
            """distancia (en el plano del techo) de la costanera al borde del sistema por ese lado, o None"""
            if not contorno:
                return None
            vs = [float(np.dot(np.array([p[0] - a[0], p[1] - a[1]]), vh)) * lado for p in contorno]
            m = max(vs)
            return max(m, 0.0) / max(math.cos(math.radians(theta)), 1e-6)
        if par.ancho_colaborante:
            s_trib = float(par.ancho_colaborante)
            origen_s = "dado en la app"
        else:
            partes = []
            for lado in (1, -1):
                vv = vecinos[lado]
                if vv is not None:
                    partes.append(vv[0] / 2)
                else:
                    otro = vecinos[-lado]
                    bd = borde(lado)
                    if otro is not None:
                        partes.append(min(bd, otro[0] / 2) if bd is not None else otro[0] / 2)
                    elif bd is not None:
                        partes.append(bd)
                    else:
                        partes.append(None)
            if all(x is None for x in partes):
                esp = sist.get("espaciamiento")
                s_trib = float(esp) if esp else 1.0
                origen_s = "separación del sistema en Revit" if esp else "sin costaneras vecinas: se usó 1 m"
            else:
                s_trib = sum(x for x in partes if x is not None)
                origen_s = "½ separación a cada lado" if all(x is not None for x in partes) and all(vecinos.values()) \
                    else "costanera de borde: ½ separación + borde del sistema"
        # tramos
        ts = [ap["t"] for ap in apoyos]
        tramos = []
        if ts:
            if ts[0] > 0.05:
                tramos.append({"tipo": "voladizo", "x0": 0.0, "x1": ts[0]})
            for t0, t1 in zip(ts, ts[1:]):
                tramos.append({"tipo": "tramo", "x0": float(t0), "x1": float(t1)})
            if L - ts[-1] > 0.05:
                tramos.append({"tipo": "voladizo", "x0": ts[-1], "x1": L})
        if len(apoyos) < 2:
            nota.append("menos de 2 apoyos: no se encontraron vigas de apoyo bajo la costanera")
        out.append({"id": c.get("id"), "sistema": c.get("sistema"), "tipo": c.get("tipo") or "", "familia": c.get("familia") or "",
                    "parametros": c.get("parametros") or {}, "L": L, "p0": a, "u": u, "uh": uh, "vh": vh,
                    "apoyos": [{"x": ap["t"], "viga": ap["viga"], "tipo": ap["tipo"]} for ap in apoyos],
                    "tramos": tramos, "s": s_trib, "origen_s": origen_s, "theta": theta, "notas": nota,
                    "z_media": float((a[2] + b[2]) / 2)})
    if z_base is None:
        z_base = (min(zs) - 3.0) if zs else 0.0
    gen = {"z_base": z_base, "pts": pts_techo, "z_max": max(zs) if zs else 0.0}
    return out, gen


# ---------------------------------------------------------------------------------------------- análisis
def _zona_elemento(cg: dict, gen: dict, xm: float, a_borde: float, theta: float) -> int:
    """Zona de viento (1, 2, 3) del punto a xm (m) a lo largo de la costanera."""
    p = cg["p0"] + cg["u"] * xm
    uh, vh = cg["uh"], cg["vh"]
    us, vs = [], []
    for a, b in gen["pts"]:
        for q in (a, b):
            us.append((q[0] - p[0]) * uh[0] + (q[1] - p[1]) * uh[1])
            vs.append((q[0] - p[0]) * vh[0] + (q[1] - p[1]) * vh[1])
    bordes = 0
    if min(-min(us), max(us)) < a_borde:                         # cerca de un borde transversal (frontón)
        bordes += 1
    cerca_v = min(-min(vs), max(vs)) < a_borde                   # alero o borde paralelo
    if theta > 7 and not cerca_v and math.tan(math.radians(theta)) > 1e-6:
        cerca_v = (gen["z_max"] - p[2]) / math.tan(math.radians(theta)) < a_borde   # cumbrera
    if cerca_v:
        bordes += 1
    return 1 + min(bordes, 2)


def analizar_costaneras(datos: dict, par: ParametrosCostaneras | None = None, avance=None) -> dict:
    par = par or ParametrosCostaneras()
    paso = avance or (lambda pct, etapa, hasta=None: None)
    paso(2, "Leyendo costaneras y apoyos", 10)
    geo, gen = _geometria(datos, par)
    pc = ParametrosCargas(v_viento=par.v_viento, exposicion=par.exposicion, kzt=par.kzt, kd=par.kd,
                          importancia_viento=par.importancia_viento)
    # altura media del techo y dimensiones en planta (para q_h y el ancho a de las zonas de viento)
    if par.h_techo:
        h = float(par.h_techo)
        origen_h = "dada en la app"
    else:
        zs = [p[2] for a, b in gen["pts"] for p in (a, b)]
        h = max(((min(zs) + max(zs)) / 2 - gen["z_base"]) if zs else 3.0, 1.0)
        origen_h = "promedio entre el punto más bajo y más alto del techo, sobre la base del modelo"
    qh = q_viento(pc, h) * 1000                                    # kgf/m²
    xy = np.array([[p[0], p[1]] for a, b in gen["pts"] for p in (a, b)]) if gen["pts"] else np.zeros((1, 2))
    resumen = [f"q_h = {qh:.1f} kgf/m² (NCh432: V = {par.v_viento:g} m/s, exposición {par.exposicion}, Kd {par.kd}, "
               f"Kzt {par.kzt}, I {par.importancia_viento}; h = {h:.2f} m, {origen_h})",
               f"Lr: Lo = {par.lo_techo:.0f} kgf/m² (NCh1537 Tabla 4)" + (", reducida por R1·R2 (§8.2) con el área tributaria de cada tramo" if par.reducir_lr else ", sin reducir"),
               f"D: cubierta {par.peso_cubierta:g} kgf/m² × ancho colaborante + peso propio del perfil",
               "Combinaciones NCh3171 §9.1.1: 1,4D; 1,2D + 1,6Lr (+0,8W); 1,2D + 1,6W + 0,5Lr; 0,9D + 1,6W"
               + ("; con nieve S: 1,2D + 1,6S + 0,8W; 1,2D + 1,6W + 0,5S" if par.nieve > 0 else ""),
               f"Tensores: {par.tensores} (apoyos del eje débil y arriostramiento del volcamiento)"
               + ("; la cubierta arriostra el ala superior con flexión positiva" if par.cubierta_arriostra else ""),
               "No se verifican: pandeo distorsional (F4), aplastamiento del alma en los apoyos (G5) ni las uniones."]
    # perfiles por tipo
    perfiles: dict[str, Perfil | None] = {}
    for cg in geo:
        tp = cg["tipo"]
        if tp in perfiles:
            continue
        txt = (par.perfiles or {}).get(tp)
        pf = leer_perfil(txt) if txt else None
        if pf is None:
            pf = leer_perfil(tp.split(":")[-1], cg["parametros"]) or leer_perfil(tp, cg["parametros"])
        if pf is None and par.perfil_defecto:
            pf = leer_perfil(par.perfil_defecto)
        if pf is not None and par.radio_interior is not None:
            pf.r = float(par.radio_interior)
        perfiles[tp] = pf
    # capacidades por perfil (las que no dependen del largo)
    caps = {}
    for tp, pf in perfiles.items():
        if pf is None:
            continue
        pr = propiedades_brutas(pf)
        mx = seccion_efectiva(pf, "x", 1, par.fy, par.fy)
        my_p = seccion_efectiva(pf, "y", 1, par.fy, par.fy)
        my_n = seccion_efectiva(pf, "y", -1, par.fy, par.fy)
        caps[tp] = {"pr": pr, "Mnxo": mx["M"], "Sex": mx["Se"], "Mny": min(my_p["M"], my_n["M"]),
                    "Vn": corte_nominal(pf, pr, par.fy)}
    a_borde_cache = {}
    res = []
    n = max(len(geo), 1)
    for k, cg in enumerate(geo):
        if k % 5 == 0:
            paso(5 + int(90 * k / n), f"Costanera {k + 1} de {len(geo)}", 5 + int(90 * (k + 5) / n))
        res.append(_disenar(cg, gen, perfiles.get(cg["tipo"]), caps.get(cg["tipo"]), par, qh, h, xy, a_borde_cache))
    paso(97, "Resumen", 100)
    # resumen por sistema
    por_sistema = {}
    for r in res:
        s = por_sistema.setdefault(r["sistema"], {"sistema": r["sistema"], "n": 0, "FU_max": 0.0, "peor": None, "perfiles": set()})
        s["n"] += 1
        s["perfiles"].add(r["perfil"] or "—")
        if r["FU"] is not None and r["FU"] >= s["FU_max"]:
            s["FU_max"], s["peor"] = r["FU"], r["id"]
    sistemas = []
    for sid, s in por_sistema.items():
        info = next((x for x in (datos.get("sistemas") or []) if x.get("id") == sid), {})
        sistemas.append({**s, "perfiles": sorted(s["perfiles"]), "nombre": info.get("nombre") or f"Sistema {sid}"})
    return {"costaneras": res, "sistemas": sistemas, "resumen": resumen, "q_h": qh, "h": h,
            "perfiles": {tp: (pf.nombre() if pf else None) for tp, pf in perfiles.items()},
            "propiedades": {tp: _props_tabla(perfiles[tp], c) for tp, c in caps.items()},
            "parametros": {k: getattr(par, k) for k in par.__dataclass_fields__}}


def _props_tabla(pf: Perfil, c: dict) -> dict:
    pr = c["pr"]
    return {"perfil": pf.nombre(), "A_cm2": pr["A"] / 100, "Ix_cm4": pr["Ix"] / 1e4, "Iy_cm4": pr["Iy"] / 1e4,
            "Sx_cm3": pr["Sx"] / 1e3, "Sex_cm3": c["Sex"] / 1e3, "peso_kgf_m": pr["peso_kgf_m"],
            "phiMnxo_kgfm": PHI_B * c["Mnxo"] / G_ACC / 1000, "phiMny_kgfm": PHI_B * c["Mny"] / G_ACC / 1000,
            "phiVn_kgf": PHI_V * c["Vn"] / G_ACC}


def _disenar(cg, gen, pf, cap, par, qh, h, xy, a_cache) -> dict:
    base = {"id": cg["id"], "sistema": cg["sistema"], "tipo": cg["tipo"], "perfil": pf.nombre() if pf else None,
            "L": cg["L"], "s": cg["s"], "origen_s": cg["origen_s"], "theta": cg["theta"],
            "apoyos": cg["apoyos"], "tramos": [], "notas": list(cg["notas"]), "FU": None, "verificaciones": []}
    if pf is None:
        base["notas"].append(f"no se reconoce el perfil del tipo «{cg['tipo']}»: escríbelo en la tabla de perfiles")
        return base
    if len(cg["apoyos"]) < 2:
        return base
    pr = cap["pr"]
    th = math.radians(cg["theta"])
    s = cg["s"]                                                    # m
    L = cg["L"]
    # nudos: extremos, apoyos, tensores y subdivisiones (m)
    puntos = {0.0, L}
    apoyos_x = [ap["x"] for ap in cg["apoyos"]]
    puntos.update(apoyos_x)
    tensores_x = []
    for tr in cg["tramos"]:
        Ls = tr["x1"] - tr["x0"]
        nt = _tensores_por_tramo(par.tensores, Ls) if tr["tipo"] == "tramo" else 0
        tr["tensores"] = nt
        for i in range(1, nt + 1):
            tensores_x.append(tr["x0"] + Ls * i / (nt + 1))
        for i in range(1, 12):
            puntos.add(tr["x0"] + Ls * i / 12)
    puntos.update(tensores_x)
    xs = np.array(sorted(puntos))
    xs = xs[np.concatenate([[True], np.diff(xs) > 1e-4])]
    idx = lambda x: int(np.argmin(np.abs(xs - x)))
    i_apoyos = {idx(x) for x in apoyos_x}
    i_tens = {idx(x) for x in tensores_x}
    xm = (xs[:-1] + xs[1:]) / 2
    ne = len(xm)
    tramo_de = np.array([next((j for j, tr in enumerate(cg["tramos"]) if tr["x0"] - 1e-6 <= x <= tr["x1"] + 1e-6), 0) for x in xm])
    # zonas de viento por elemento
    us_all = xy @ cg["uh"]
    vs_all = xy @ cg["vh"]
    menor = max(min(us_all.max() - us_all.min(), vs_all.max() - vs_all.min()), 1.0)
    a_b = max(min(0.1 * menor, 0.4 * h), 0.04 * menor, 0.9)
    if par.zona_viento in ("1", "2", "3"):
        zonas = np.full(ne, int(par.zona_viento))
    else:
        zonas = np.array([_zona_elemento(cg, gen, x, a_b, cg["theta"]) for x in xm])
    # cargas por elemento (N/mm, positivas hacia la superficie del techo = hacia abajo)
    peso = pr["peso_kgf_m"] * G_ACC / 1000                           # N/mm
    d_lin = par.peso_cubierta * KGF_M2 * s * 1000 + peso              # vertical, N/mm
    lr_el = np.zeros(ne)
    wpos, wneg = np.zeros(ne), np.zeros(ne)
    lr_info, w_info = [], []
    for j, tr in enumerate(cg["tramos"]):
        Ls = tr["x1"] - tr["x0"]
        at = Ls * s
        red = reduccion_lr(at, math.tan(th) * 100)
        f = red["factor"] if par.reducir_lr else 1.0
        lr = max(par.lo_techo * f, 300 / G_ACC)                       # kgf/m² horizontal
        lr_el[tramo_de == j] = lr * KGF_M2 * s * math.cos(th) * 1000
        lr_info.append(lr)
        area = Ls * max(s, Ls / 3) if tr["tipo"] == "tramo" else 2 * Ls * max(s, 2 * Ls / 3)
        gp = gcp(cg["theta"], "pos", area)
        for e in np.where(tramo_de == j)[0]:
            gn = gcp(cg["theta"], int(zonas[e]), area)
            pp = max(qh * (gp + par.gcpi), 49.0)                      # kgf/m², hacia la superficie; mín. 0,48 kPa
            pn = min(qh * (gn - par.gcpi), -49.0)                     # succión (negativa)
            wpos[e] = pp * KGF_M2 * s * 1000
            wneg[e] = pn * KGF_M2 * s * 1000
        w_info.append({"area": area, "GCp_pos": gp,
                       "GCp_neg": {int(z): gcp(cg["theta"], int(z), area) for z in sorted(set(zonas[tramo_de == j]))}})
    s_el = np.full(ne, par.nieve * KGF_M2 * s * math.cos(th) * 1000)
    # casos: (normal al techo, paralela) por unidad; positivas hacia abajo / hacia la parte baja del techo
    casos = {"D": (np.full(ne, d_lin * math.cos(th)), np.full(ne, d_lin * math.sin(th))),
             "Lr": (lr_el * math.cos(th), lr_el * math.sin(th)),
             "W+": (wpos, np.zeros(ne)), "W-": (wneg, np.zeros(ne))}
    if par.nieve > 0:
        casos["S"] = (s_el * math.cos(th), s_el * math.sin(th))
    EIx = E_AISI * pr["Ix"]
    EIy = E_AISI * pr["Iy"]
    xs_mm = xs * 1000
    sol = {}
    for nombre, (qn, qp) in casos.items():
        Mx, Vx, vx, Rx = viga_continua(xs_mm, i_apoyos, -qn, EIx)
        My, Vy, vy, Ry = viga_continua(xs_mm, i_apoyos | i_tens, -qp, EIy)
        sol[nombre] = {"Mx": Mx, "Vx": Vx, "vx": vx, "My": My, "Rx": Rx}
    combos = {"1,4D": {"D": 1.4}, "1,2D+1,6Lr": {"D": 1.2, "Lr": 1.6}}
    for w in ("W+", "W-"):
        combos[f"1,2D+1,6Lr+0,8{w}"] = {"D": 1.2, "Lr": 1.6, w: 0.8}
        combos[f"1,2D+1,6{w}+0,5Lr"] = {"D": 1.2, w: 1.6, "Lr": 0.5}
        combos[f"0,9D+1,6{w}"] = {"D": 0.9, w: 1.6}
        if par.nieve > 0:
            combos[f"1,2D+1,6S+0,8{w}"] = {"D": 1.2, "S": 1.6, w: 0.8}
            combos[f"1,2D+1,6{w}+0,5S"] = {"D": 1.2, w: 1.6, "S": 0.5}
    if par.nieve > 0:
        combos["1,2D+1,6S"] = {"D": 1.2, "S": 1.6}

    def comb(cmb, clave):
        return sum(f * sol[c][clave] for c, f in cmb.items())
    # segmentos entre arriostramientos laterales (apoyos y tensores) para el volcamiento
    frenos = sorted(i_apoyos | i_tens)
    segmentos = []
    for i0, i1 in zip(frenos, frenos[1:]):
        segmentos.append((i0, i1, False))
    if frenos and frenos[0] > 0:
        segmentos.insert(0, (0, frenos[0], True))
    if frenos and frenos[-1] < len(xs) - 1:
        segmentos.append((frenos[-1], len(xs) - 1, True))
    phiMnxo = PHI_B * cap["Mnxo"]
    phiMny = PHI_B * cap["Mny"]
    phiVn = PHI_V * cap["Vn"]
    peor = {"biaxial": (0, None, None, None), "corte": (0, None, None), "flexion_corte": (0, None, None)}
    for nombre, cmb in combos.items():
        Mx, My, Vx = comb(cmb, "Mx"), comb(cmb, "My"), comb(cmb, "Vx")
        phiMnx = np.full(len(xs), phiMnxo)
        info_seg = np.empty(len(xs), dtype=object)
        for i0, i1, volado in segmentos:
            Lb = (xs[i1] - xs[i0]) * 1000
            Cb = 1.0 if volado else _cb(Mx[i0:i1 + 1])
            ltb = fn_volcamiento(pf, pr, Lb, Cb, par.fy)
            if ltb["Fn"] < par.fy - 1e-6:
                se = seccion_efectiva(pf, "x", 1, ltb["Fn"], par.fy)
                mn = min(se["M"], cap["Mnxo"])
            else:
                mn = cap["Mnxo"]
            for i in range(i0, i1 + 1):
                pos = Mx[i] > 0
                val = phiMnxo if (pos and par.cubierta_arriostra) else PHI_B * mn
                if info_seg[i] is None or val < phiMnx[i]:
                    phiMnx[i] = val
                    info_seg[i] = {"Lb_m": Lb / 1000, "Cb": Cb, "Fe": ltb["Fe"], "Fn": ltb["Fn"]}
        r = np.abs(Mx) / phiMnx + np.abs(My) / phiMny
        i = int(np.argmax(r))
        if r[i] > peor["biaxial"][0]:
            peor["biaxial"] = (float(r[i]), nombre, i, info_seg[i], float(Mx[i]), float(My[i]), float(phiMnx[i]))
        rv = np.abs(Vx) / phiVn
        i = int(np.argmax(rv))
        if rv[i] > peor["corte"][0]:
            peor["corte"] = (float(rv[i]), nombre, i, float(Vx[i]))
        rmv = np.sqrt((Mx / phiMnxo) ** 2 + (Vx / phiVn) ** 2)
        # H2 se verifica donde actúan juntos: en los apoyos
        ia = list(i_apoyos)
        j = ia[int(np.argmax(rmv[ia]))]
        if rmv[j] > peor["flexion_corte"][0]:
            peor["flexion_corte"] = (float(rmv[j]), nombre, j, float(Mx[j]), float(Vx[j]))
    kgfm = lambda m: m / G_ACC / 1000                                # N·mm → kgf·m
    kgf = lambda v: v / G_ACC
    ver = []
    b = peor["biaxial"]
    if b[1]:
        ver.append({"verificacion": "Flexión biaxial (H1.2): Mx/φMnx + My/φMny", "razon": b[0], "combo": b[1], "x": float(xs[b[2]]),
                    "demanda": f"Mx {kgfm(b[4]):.0f} kgf·m, My {kgfm(b[5]):.0f} kgf·m",
                    "capacidad": f"φMnx {kgfm(b[6]):.0f}, φMny {kgfm(phiMny):.0f} kgf·m",
                    "notas": ([f"volcamiento: Lb {b[3]['Lb_m']:.2f} m, Cb {b[3]['Cb']:.2f}" +
                               (f", Fe {b[3]['Fe']:.0f} MPa, Fn {b[3]['Fn']:.0f} MPa" if b[3]['Fe'] else "")] if b[3] else [])})
    c = peor["corte"]
    if c[1]:
        ver.append({"verificacion": "Corte (G2.1)", "razon": c[0], "combo": c[1], "x": float(xs[c[2]]),
                    "demanda": f"Vu {kgf(abs(c[3])):.0f} kgf", "capacidad": f"φVn {kgf(phiVn):.0f} kgf", "notas": []})
    m = peor["flexion_corte"]
    if m[1]:
        ver.append({"verificacion": "Flexión y corte en el apoyo (H2)", "razon": m[0], "combo": m[1], "x": float(xs[m[2]]),
                    "demanda": f"Mx {kgfm(m[3]):.0f} kgf·m, V {kgf(abs(m[4])):.0f} kgf",
                    "capacidad": f"φMnxo {kgfm(phiMnxo):.0f} kgf·m, φVn {kgf(phiVn):.0f} kgf", "notas": []})
    # deformaciones (servicio, normal al techo, Ix bruta)
    for etiqueta, cmb, lim in (("D + Lr", {"D": 1, "Lr": 1}, par.limite_gravedad),
                               ("W presión", {"W+": 1}, par.limite_viento), ("W succión", {"W-": 1}, par.limite_viento)):
        v = comb(cmb, "vx")
        peor_d = (0.0, None, 0.0, 0.0)
        for tr in cg["tramos"]:
            sel = (xs >= tr["x0"] - 1e-6) & (xs <= tr["x1"] + 1e-6)
            Ls = tr["x1"] - tr["x0"]
            Lref = Ls if tr["tipo"] == "tramo" else 2 * Ls
            if Lref <= 0:
                continue
            vt = v[sel]
            if tr["tipo"] == "tramo":
                v0, v1 = vt[0], vt[-1]
                rel = vt - (v0 + (v1 - v0) * (xs[sel] - xs[sel][0]) / max(Ls, 1e-9))
            else:
                rel = vt
            dmax = float(np.max(np.abs(rel)))
            r = float(dmax / (Lref * 1000 / lim))
            if r > peor_d[0]:
                peor_d = (r, tr, dmax, Lref)
        if peor_d[1] is not None:
            ver.append({"verificacion": f"Deformación {etiqueta} (L/{lim:g})", "razon": peor_d[0], "combo": etiqueta,
                        "x": float((peor_d[1]["x0"] + peor_d[1]["x1"]) / 2),
                        "demanda": f"δ {peor_d[2]:.1f} mm", "capacidad": f"L/{lim:g} = {peor_d[3] * 1000 / lim:.1f} mm",
                        "notas": ["voladizo: L = 2·largo"] if peor_d[1]["tipo"] == "voladizo" else []})
    # diagramas (envolvente de Mx mayorado) para dibujar
    env_max = np.max([comb(cmb, "Mx") for cmb in combos.values()], axis=0)
    env_min = np.min([comb(cmb, "Mx") for cmb in combos.values()], axis=0)
    paso_d = max(1, len(xs) // 80)
    tramos_out = []
    for j, tr in enumerate(cg["tramos"]):
        tramos_out.append({**{k: (float(v) if isinstance(v, (float, np.floating)) else v) for k, v in tr.items()},
                           "luz": float(tr["x1"] - tr["x0"]), "Lr_kgf_m2": lr_info[j], **w_info[j],
                           "zonas": sorted({int(z) for z in zonas[tramo_de == j]})})
    reacciones = {}
    for nombre in casos:
        reacciones[nombre] = [float(kgf(sol[nombre]["Rx"][i])) for i in sorted(i_apoyos)]
    base.update({
        "tramos": tramos_out, "tensores_x": sorted(tensores_x), "verificaciones": ver,
        "FU": max((v["razon"] for v in ver), default=None),
        "cargas": {"D_kgf_m": d_lin * 1000 / G_ACC, "peso_propio_kgf_m": pr["peso_kgf_m"],
                   "Lr_kgf_m": float(np.max(lr_el)) * 1000 / G_ACC,
                   "W_pos_kgf_m": float(np.max(wpos)) * 1000 / G_ACC, "W_neg_kgf_m": float(np.min(wneg)) * 1000 / G_ACC,
                   "a_borde_m": a_b},
        "reacciones_servicio_kgf": reacciones,
        "diagrama": {"x": [float(x) for x in xs[::paso_d]] + ([float(xs[-1])] if (len(xs) - 1) % paso_d else []),
                     "Mmax": [kgfm(v) for v in env_max[::paso_d]] + ([kgfm(env_max[-1])] if (len(xs) - 1) % paso_d else []),
                     "Mmin": [kgfm(v) for v in env_min[::paso_d]] + ([kgfm(env_min[-1])] if (len(xs) - 1) % paso_d else [])},
    })
    if any(t["tipo"] == "voladizo" for t in tramos_out):
        base["notas"].append("tiene voladizo(s)")
    return base
