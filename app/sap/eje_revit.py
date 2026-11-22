"""Modelo 2D de un eje del modelo de Revit (archivo ``eje-*.sap.json`` del botón «Eje a SAP2000» de cad2revit).

- Pilares, vigas y diagonales de acero como barras (secciones I o cajón según las dimensiones de Revit).
- Muros de marco plataforma como shell de membrana con la rigidez lateral equivalente del *Manual de diseño de
  estructuras en madera* (Ec. 2.22, formato SDPWS-2015):

      K_eq = [ 2/3 · H³/(E·A_ext·L²)  +  H/(G_a·L)  +  H²/(L·L'·K_anclaje) ]⁻¹

  El shell de cada segmento de muro de altura completa (entre vanos) se calibra para que su rigidez lateral como
  voladizo sea K_eq: el término de flexión de los pies derechos de borde fija E del shell y los de corte (tablero y
  clavos) y volcamiento (anclaje) fijan G.
- Muros de hormigón como shell de membrana con E del hormigón.

Todo en tonf y m. El mallado y el archivo .s2k son Python puro (sin SAP2000); ``construir_en_sap`` arma el modelo
en SAP2000 vía COM (solo Windows).

Uso::

    python -m sap2000_toolkit.eje_revit eje-C.sap.json --s2k eje-C.s2k     # para Archivo → Importar en SAP2000
    python -m sap2000_toolkit.eje_revit eje-C.sap.json --sap --guardar C:\\modelos\\eje-C.sdb
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, field

G_ACC = 9.80665

# Rigidez de corte aparente Ga [tonf/m] de muros con 1 tablero OSB «Sheathing» 9,5 mm y clavo 8d, según el
# espaciamiento del clavado perimetral [mm] (Manual de diseño de estructuras en madera, Tablas 18 y 19, de la
# Tabla 4.3A del SDPWS-2015). Con tablero en ambas caras se duplica.
GA_OSB_9_5_8D = {150: 304.0, 100: 446.0, 50: 804.0}


# Hold-downs del Manual de diseño de estructuras en madera (Tablas 20, 21 y 47; Simpson Strong-Tie serie HDB, valores
# SPF/HF para pino radiata, ya amplificados por 1,6 por carga de corta duración): modelo → (T_adm tonf, Δ_adm m, b_HD m)
HOLD_DOWNS = {
    "HD3B": (1.38, 0.0030, 0.0333),
    "HD5B": (1.90, 0.0038, 0.0318),
    "HD7B": (2.56, 0.0036, 0.0318),
    "HD9B": (3.82, 0.0045, 0.0318),
    "HD12": (5.48, 0.0041, 0.0540),
    "HD19": (7.35, 0.0035, 0.0540),
    "HD12 dobles": (8.36, 0.0043, 0.0540),
}


def hold_down_para(T: float) -> str | None:
    """El hold-down más chico del catálogo que resiste la tracción T (tonf); None si ninguno alcanza."""
    for nombre, (t_adm, _, _) in sorted(HOLD_DOWNS.items(), key=lambda kv: kv[1][0]):
        if t_adm >= T:
            return nombre
    return None


@dataclass
class ParametrosMuroMadera:
    """Parámetros de la rigidez lateral de un muro de marco plataforma (tonf, m)."""

    e_ppdd: float = 1.02e6              # E de los pies derechos de borde: 102 tonf/cm²
    a_ext: float = 2 * 0.05 * 0.075     # área de los PPDD de un extremo: 2 piezas 2x3 (50x75 mm)
    b_ext: float = 0.10                 # ancho de los PPDD de borde (L' = L − b_ext)
    espaciamiento_clavos: int = 100     # mm, clavado perimetral del tablero
    tableros: int = 1                   # caras con OSB
    ga: float | None = None             # tonf/m; si se da, manda sobre la tabla
    k_anclaje: float = 5.48 / 0.0041    # hold-down HD12: T_adm 5,48 tonf / Δ_adm 4,1 mm ≈ 1337 tonf/m
    # anclaje al volcamiento (todo muro va anclado): "pilar" (por defecto): el borde que llega a un pilar de acero se
    # une a él (el pilar toma la tracción) y el borde que llega a un vano lleva hold-down; "hold_down": ambos bordes
    anclaje: str = "pilar"
    hold_down: str = "auto"             # modelo de HOLD_DOWNS; "auto" = el más chico que resiste (rigidez: k_anclaje)
    k_union_pilar: float = 5.48 / 0.0041  # tonf/m, rigidez axial de la unión del PPDD de borde al pilar
    t_union_pilar: float | None = None  # tonf, tracción admisible de esa unión (pernos o tornillos al pilar)
    peso: float = 0.05                  # tonf/m² de muro (peso propio)

    def ga_total(self) -> float:
        if self.ga is not None:
            return self.ga
        if self.espaciamiento_clavos not in GA_OSB_9_5_8D:
            raise ValueError(f"Sin Ga tabulado para clavado cada {self.espaciamiento_clavos} mm; "
                             f"usa {sorted(GA_OSB_9_5_8D)} o da 'ga' directamente")
        return GA_OSB_9_5_8D[self.espaciamiento_clavos] * self.tableros


def rigidez_muro(L: float, H: float, p: ParametrosMuroMadera) -> dict:
    """Rigidez lateral equivalente (Ec. 2.22) de un segmento de muro de largo L y altura H [m]: tonf/m, con sus
    tres flexibilidades [m/tonf]."""
    lp = max(L - p.b_ext, 0.5 * L)
    f_flex = 2.0 / 3.0 * H ** 3 / (p.e_ppdd * p.a_ext * L ** 2)
    f_corte = H / (p.ga_total() * L)
    f_volc = H ** 2 / (L * lp * p.k_anclaje)
    return {"K": 1.0 / (f_flex + f_corte + f_volc), "flexion": f_flex, "corte": f_corte, "volcamiento": f_volc}


def shell_equivalente(L: float, H: float, t: float, p: ParametrosMuroMadera) -> tuple[float, float, dict]:
    """E y G [tonf/m²] de un shell de membrana de espesor t que, como voladizo de L × H, tenga la rigidez K_eq:
    flexión del shell 4H³/(E t L³) = flexión de los PPDD; corte del shell 1,2 H/(G t L) = corte + volcamiento."""
    r = rigidez_muro(L, H, p)
    e = 4.0 * H ** 3 / (t * L ** 3 * r["flexion"])
    g = 1.2 * H / (t * L * (r["corte"] + r["volcamiento"]))
    return e, g, r


# ------------------------------------------------------------------------------------------------ modelo plano

@dataclass
class ModeloEje:
    nombre: str
    nudos: dict = field(default_factory=dict)          # nombre → (x, z)
    apoyos: dict = field(default_factory=dict)         # nombre → "rotula" | "empotrado" | "x" (solo horizontal)
    barras: list = field(default_factory=list)         # (nombre, ni, nj, seccion)
    areas: list = field(default_factory=list)          # (nombre, [n1..n4], seccion_area)
    materiales: dict = field(default_factory=dict)     # nombre → {tipo, E, nu, gamma}
    secciones: dict = field(default_factory=dict)      # nombre → {forma, material, d, bf, tw, tf, t}
    secciones_area: dict = field(default_factory=dict) # nombre → {material, espesor, f11, f22, f12, peso}
    avisos: list = field(default_factory=list)
    resumen_muros: list = field(default_factory=list)
    roles: dict = field(default_factory=dict)         # barra → "pilar" | "viga" | "techo" | "diagonal"
    origen: dict = field(default_factory=dict)        # barra / área → id del elemento de Revit
    ancho_tributario: float = 0.0


ACERO = {"tipo": "Steel", "E": 2.0e8 / G_ACC, "nu": 0.3, "gamma": 7.85}            # 200 GPa
HORMIGON = {"tipo": "Concrete", "E": 4700 * math.sqrt(25) * 1000 / G_ACC, "nu": 0.2, "gamma": 2.5}


def _seccion(m: dict, s: dict, avisos: list, cambios: dict | None = None) -> str:
    nombre = (m.get("seccion") or "SIN_NOMBRE").replace(" ", "_")
    if nombre in s:
        return nombre
    d = dict(m.get("dims") or {})
    d.update((cambios or {}).get(nombre) or {})
    if all(k in d for k in ("d", "bf", "tw", "tf")):
        s[nombre] = {"forma": "I", "material": "ACERO", "d": d["d"], "bf": d["bf"], "tw": d["tw"], "tf": d["tf"]}
    elif "d" in d and "bf" in d and "t" in d:
        s[nombre] = {"forma": "Box", "material": "ACERO", "d": d["d"], "bf": d["bf"], "t": d["t"]}
    else:
        s[nombre] = {"forma": "I", "material": "ACERO", "d": 0.20, "bf": 0.10, "tw": 0.006, "tf": 0.008}
        avisos.append(f"Sección {nombre}: Revit no dio sus dimensiones; se usó una I 200x100x6x8 (revísala en SAP2000)")
    return nombre


def _juntar(candidatos: list, tol: float):
    """Agrupa coordenadas a menos de tol entre sí; cada grupo queda en la de mayor prioridad (número menor).
    Devuelve una función que lleva cualquier coordenada de la lista a la de su grupo."""
    orden = sorted(candidatos)
    grupos, actual = [], []
    for v, pr in orden:
        if actual and v - actual[-1][0] > tol:
            grupos.append(actual)
            actual = []
        actual.append((v, pr))
    if actual:
        grupos.append(actual)
    mapa = []
    for g in grupos:
        rep = min(g, key=lambda t: (t[1], t[0]))[0]
        mapa.append((g[0][0], g[-1][0], rep))

    def aj(v):
        for a, b, rep in mapa:
            if a - 1e-9 <= v <= b + 1e-9:
                return rep
        return v
    return aj


def mallar(eje: dict, pm: ParametrosMuroMadera | None = None, *, malla: float = 0.5, apoyo_pilares: str = "rotula",
           tol_borde: float = 0.40, opciones: dict | None = None, min_celda: float = 0.10) -> ModeloEje:
    """Arma el modelo plano (x, z) del eje: nudos compartidos entre barras y shells, apoyos en la base.

    ``opciones`` (ajustes de la aplicación web): ``excluir`` [ids de Revit], ``apoyos`` {id pilar: "rotula" |
    "empotrado"}, ``secciones`` {nombre: {d, bf, tw, tf} o {d, bf, t}}, ``muros`` {id muro: {espaciamiento_clavos,
    tableros, ga, k_anclaje, a_ext, peso}}.
    """
    from dataclasses import replace as _replace
    pm = pm or ParametrosMuroMadera()
    op = opciones or {}
    excluir = {str(x) for x in op.get("excluir") or []}
    mod = ModeloEje(nombre=f"Eje {eje.get('eje', '')}")
    mod.materiales["ACERO"] = dict(ACERO)
    claves = {}

    def nudo(x, z):
        k = (round(x, 3), round(z, 3))
        if k not in claves:
            claves[k] = f"N{len(claves) + 1}"
            mod.nudos[claves[k]] = (round(x, 4), round(z, 4))
        return claves[k]

    # el hormigón (fundaciones, sobrecimientos, pedestales) no entra en el modelo: solo acero y muros de madera
    pilares = [p for p in eje.get("pilares", []) if p.get("material") not in ("madera", "hormigon") and str(p.get("id")) not in excluir]
    vigas = [dict(v) for v in eje.get("vigas", []) if v.get("material") not in ("madera", "hormigon") and str(v.get("id")) not in excluir]
    xs_pilar = sorted({round((p["x0"] + p["x1"]) / 2, 4) for p in pilares})

    def a_pilar(x):
        c = min(xs_pilar, key=lambda v: abs(v - x), default=None)
        return c if c is not None and abs(c - x) <= tol_borde else x

    def viga_en(x, z):
        """z de la viga que pasa por x cerca de z (para unir el borde superior del muro a ella)."""
        mejor = None
        for v in vigas:
            x0, x1 = sorted((v["x0"], v["x1"]))
            if not (x0 - 1e-6 <= x <= x1 + 1e-6) or abs(v["x1"] - v["x0"]) < 1e-6:
                continue
            zv = v["z0"] + (v["z1"] - v["z0"]) * (x - v["x0"]) / (v["x1"] - v["x0"])
            if abs(zv - z) <= tol_borde and (mejor is None or abs(zv - z) < abs(mejor - z)):
                mejor = zv
        return mejor if mejor is not None else z

    # ---- muros: malla de cuadriláteros (sin las celdas de los vanos) ----
    for w in eje.get("muros", []):
        if str(w.get("id")) in excluir or w.get("material") == "hormigon":
            continue
        pmw = _replace(pm, **{k: v for k, v in (op.get("muros") or {}).get(str(w.get("id")), {}).items()
                             if k in ParametrosMuroMadera.__dataclass_fields__ and v not in (None, "")})
        perfil = sorted((float(a), float(b)) for a, b in w.get("perfil") or [])
        x0, x1 = a_pilar(w["x0"]), a_pilar(w["x1"])
        if x1 - x0 < 0.2 or len(perfil) < 2:
            continue
        z0 = w["z0"]
        # muro sobre una viga (p. ej. en un piso superior): su base se une a la viga y no lleva apoyos
        z0v = viga_en((x0 + x1) / 2, z0)
        sobre_viga = abs(z0v - z0) > 1e-9 or any(
            abs(v["z0"] - z0) < 0.02 and abs(v["z1"] - z0) < 0.02 and min(v["x0"], v["x1"]) <= (x0 + x1) / 2 <= max(v["x0"], v["x1"])
            for v in vigas)
        z0 = z0v

        def tope(x):
            if x <= perfil[0][0]:
                zt = perfil[0][1]
            elif x >= perfil[-1][0]:
                zt = perfil[-1][1]
            else:
                zt = next(za + (zb - za) * (x - xa) / (xb - xa)
                          for (xa, za), (xb, zb) in zip(perfil, perfil[1:]) if xa <= x <= xb and xb > xa)
            return viga_en(x, zt)

        vanos = [dict(v) for v in w.get("vanos", []) if v["x1"] > x0 + 0.05 and v["x0"] < x1 - 0.05]
        # líneas verticales: pilares, extremos, quiebres del borde superior y bordes de vanos. Las que quedan a menos de
        # min_celda se juntan (manda el pilar, luego el extremo, el quiebre y al final el vano); entre ellas, ≈ malla
        cand = [(x, 0) for x in xs_pilar if x0 - 1e-6 <= x <= x1 + 1e-6] + [(x0, 1), (x1, 1)]
        cand += [(x, 2) for x, _ in perfil if x0 < x < x1]
        # líneas donde el techo cruza cada nivel horizontal (cada «malla» sobre el alero): ahí cada franja termina en
        # un solo triángulo bajo el techo
        npt = max(2, int((x1 - x0) / 0.02))
        xp = [x0 + (x1 - x0) * i / npt for i in range(npt + 1)]
        zp = [tope(x) for x in xp]
        zal, zcum = min(zp), max(zp)
        nivel = zal + malla
        while nivel < zcum - min_celda / 2:
            for (xa_, za_), (xb_, zb_) in zip(zip(xp, zp), zip(xp[1:], zp[1:])):
                if (za_ - nivel) * (zb_ - nivel) < 0:
                    cand.append((xa_ + (xb_ - xa_) * (nivel - za_) / (zb_ - za_), 1))
            nivel += malla
        cand += [(min(max(v[k], x0), x1), 3) for v in vanos for k in ("x0", "x1")]
        ajx = _juntar(cand, min_celda)
        for v in vanos:
            v["x0"], v["x1"] = ajx(min(max(v["x0"], x0), x1)), ajx(min(max(v["x1"], x0), x1))
        vanos = [v for v in vanos if v["x1"] - v["x0"] > 0.05]
        cx = sorted({ajx(x) for x, _ in cand})
        xs = []
        for a, b in zip(cx, cx[1:]):
            n = max(1, round((b - a) / malla))
            xs += [a + (b - a) * i / n for i in range(n)]
        xs.append(cx[-1])
        tops = [tope(x) for x in xs]
        zmin_top, zmax_top = min(tops), max(tops)
        # niveles HORIZONTALES comunes a todo el muro: base, bordes de vanos, alero (tope más bajo) y, hasta la
        # cumbrera, cada ≈ malla
        candz = [(z0, 0), (zmin_top, 1)] + [(min(max(v[k], z0), zmax_top), 3) for v in vanos for k in ("z0", "z1")]
        # vigas horizontales que cruzan el muro a media altura: su nivel es una línea de la malla (quedan unidas al muro)
        candz += [((v["z0"] + v["z1"]) / 2, 1) for v in vigas if abs(v["z1"] - v["z0"]) < 0.02
                  and z0 + min_celda < (v["z0"] + v["z1"]) / 2 < zmin_top - min_celda
                  and min(v["x0"], v["x1"]) < x1 - 1e-6 and max(v["x0"], v["x1"]) > x0 + 1e-6]
        ajz = _juntar(candz, min_celda)
        for v in vanos:
            v["z0"], v["z1"] = ajz(min(max(v["z0"], z0), zmax_top)), ajz(min(max(v["z1"], z0), zmax_top))
        cz = sorted({ajz(z) for z, _ in candz})
        zs = []
        for a, b in zip(cz, cz[1:]):
            n = max(1, math.ceil((b - a) / malla - 1e-9))
            zs += [a + (b - a) * k / n for k in range(n)]
        zs.append(cz[-1])
        z = zmin_top
        while z + malla < zmax_top - min_celda / 2:
            z += malla
            if z > zs[-1] + 1e-6:
                zs.append(z)
        if zmax_top > zs[-1] + 1e-6:
            zs.append(zmax_top)                     # hasta la cumbrera
        # el borde superior se lleva al nivel cercano (< min_celda/2): sin triángulos muy delgados
        for i, t in enumerate(tops):
            cerca = min(zs, key=lambda zz: abs(zz - t))
            if abs(cerca - t) < min_celda / 2:
                tops[i] = cerca
        zmin_top = min(tops)

        # material del muro: por segmento de altura completa (entre vanos)
        es_madera = w.get("material") == "madera" or w.get("entramado")
        t = max(float(w.get("nucleo") or 0.075), 0.05)
        H = sum(tops) / len(tops) - z0
        segmentos = []
        borde = [x0] + sorted({v["x0"] for v in vanos} | {v["x1"] for v in vanos}) + [x1]
        for a, b in zip(borde, borde[1:]):
            if b - a > 0.1 and not any(v["x0"] < (a + b) / 2 < v["x1"] for v in vanos):
                segmentos.append((a, b))
        if not segmentos:
            mod.avisos.append(f"Muro {w['id']}: sin segmentos de altura completa; no se modela")
            continue

        def seccion_area(xm):
            seg = min(segmentos, key=lambda s: 0 if s[0] <= xm <= s[1] else min(abs(xm - s[0]), abs(xm - s[1])))
            L = seg[1] - seg[0]
            if not es_madera:
                mat = "HORMIGON" if w.get("material") == "hormigon" else "ACERO"
                mod.materiales.setdefault(mat, dict(HORMIGON if mat == "HORMIGON" else ACERO))
                nombre = f"MURO_{mat}_{round(t * 1000)}"
                mod.secciones_area.setdefault(nombre, {"material": mat, "espesor": t, "f11": 1, "f22": 1, "f12": 1, "peso": 1})
                return nombre
            # anclaje de cada borde del segmento: unión al pilar (si el muro va unido a pilares y hay uno en ese
            # borde) o hold-down; la rigidez del anclaje entra en la calibración del shell (Manual, Ec. 2.22)
            en_pilar = [any(abs(xb - xp) <= 0.05 for xp in xs_pilar) for xb in seg]
            bordes = ["pilar" if (pmw.anclaje == "pilar" and p_) else "hold_down" for p_ in en_pilar]
            k_hd = (HOLD_DOWNS[pmw.hold_down][0] / HOLD_DOWNS[pmw.hold_down][1]) if pmw.hold_down in HOLD_DOWNS else pmw.k_anclaje
            k_anc = min(pmw.k_union_pilar if b_ == "pilar" else k_hd for b_ in bordes)
            e, g, r = shell_equivalente(L, H, t, _replace(pmw, k_anclaje=k_anc))
            mod.materiales.setdefault("MADERA_EQ", {"tipo": "Other", "E": pmw.e_ppdd, "nu": 0.0, "gamma": pmw.peso / t})
            g0 = pmw.e_ppdd / 2.0
            nombre = f"MPF_{w['id']}_{round(seg[0] * 100)}"
            if nombre not in mod.secciones_area:
                mod.secciones_area[nombre] = {"material": "MADERA_EQ", "espesor": t, "f11": e / pmw.e_ppdd,
                                              "f22": e / pmw.e_ppdd, "f12": g / g0, "peso": 1}
                mod.resumen_muros.append({"muro": w["id"], "x0": round(seg[0], 3), "x1": round(seg[1], 3), "L": round(L, 3),
                                          "H": round(H, 3), "K_eq_tonf_m": round(r["K"], 1), "E_shell": round(e, 1),
                                          "G_shell": round(g, 1), "seccion": nombre,
                                          "parametros": {"espaciamiento_clavos": pmw.espaciamiento_clavos,
                                                         "tableros": pmw.tableros, "b_ext": pmw.b_ext},
                                          "anclaje": {"bordes": bordes, "hold_down": pmw.hold_down, "k": round(k_anc, 1),
                                                      "t_union_pilar": pmw.t_union_pilar}})
            return nombre

        nudos_muro = set()
        # bordes del muro unidos a un pilar (anclaje «pilar»): la esquina inferior del muro es un nudo propio, sin apoyo
        # vertical, y el volcamiento baja por el pilar (muro solidario con el pilar, como el par de bordes del Manual)
        bordes_seg = sorted({round(x_, 4) for s_ in segmentos for x_ in s_})
        bordes_pilar = [xb for xb in bordes_seg if pmw.anclaje == "pilar" and not sobre_viga
                        and any(abs(xb - xp) <= 0.05 for xp in xs_pilar)]
        esquinas = set()

        def nudo_muro(x, zz):
            if abs(zz - z0) < 1e-6 and any(abs(x - xb) <= 0.05 for xb in bordes_pilar):
                k = (round(x, 3), round(zz, 3), f"muro {w.get('id')}")
                if k not in claves:
                    claves[k] = f"N{len(claves) + 1}"
                    mod.nudos[claves[k]] = (round(x, 4), round(zz, 4))
                esquinas.add(claves[k])
                return claves[k]
            return nudo(x, zz)

        def area(pts, xm):
            ns = []
            for x, zz in pts:
                n = nudo_muro(x, zz)
                if n not in ns:
                    ns.append(n)
            if len(ns) < 3:
                return
            q = [mod.nudos[n] for n in ns]
            if abs(sum(q[k][0] * q[(k + 1) % len(q)][1] - q[(k + 1) % len(q)][0] * q[k][1] for k in range(len(q)))) / 2 < 1e-4:
                return
            mod.areas.append((f"A{len(mod.areas) + 1}", ns, seccion_area(xm)))
            mod.origen[mod.areas[-1][0]] = str(w.get("id"))
            nudos_muro.update(ns)

        for i in range(len(xs) - 1):
            xa, xb, ta, tb = xs[i], xs[i + 1], tops[i], tops[i + 1]
            xm = (xa + xb) / 2
            for k in range(len(zs) - 1):
                za_, zb_ = zs[k], zs[k + 1]
                if za_ >= max(ta, tb) - 1e-9:
                    break
                zm = (za_ + zb_) / 2
                if any(v["x0"] < xm < v["x1"] and v["z0"] < zm < v["z1"] for v in vanos):
                    continue
                rect = [(xa, za_), (xb, za_), (xb, zb_), (xa, zb_)]
                if zb_ <= min(ta, tb) + 1e-9:
                    area(rect, xm)
                    continue
                # celda que cruza el borde inclinado: se recorta con la recta del borde (z ≤ borde)
                def bajo(pt):
                    return pt[1] - (ta + (tb - ta) * (pt[0] - xa) / (xb - xa)) <= 1e-9
                poli = []
                for a_, b_ in zip(rect, rect[1:] + rect[:1]):
                    if bajo(a_):
                        poli.append(a_)
                    if bajo(a_) != bajo(b_):
                        # cruce del lado a_-b_ con la recta del borde
                        if abs(a_[0] - b_[0]) < 1e-12:                        # lado vertical
                            poli.append((a_[0], ta if abs(a_[0] - xa) < 1e-12 else tb))
                        else:                                                  # lado horizontal z = a_[1]
                            xc = xa + (a_[1] - ta) * (xb - xa) / (tb - ta)
                            poli.append((xc, a_[1]))
                if len(poli) == 5:
                    area(poli[:4], xm)
                    area([poli[0], poli[3], poli[4]], xm)
                else:
                    area(poli, xm)
        if not sobre_viga:
            # apoyos solo en los nudos de la base que pertenecen al muro (no en el vano de una puerta): en toda la base se
            # restringe el deslizamiento (conectores antideslizamiento); el apoyo vertical va en los bordes de cada
            # segmento que no están unidos a un pilar (hold-down y PPDD de borde). En los bordes unidos a un pilar el
            # vertical lo da el pilar, y en el interior de la base el volcamiento queda como el par de los bordes
            for n in nudos_muro:
                x_, z_ = mod.nudos[n]
                if abs(z_ - z0) < 1e-6:
                    borde = n not in esquinas and any(abs(x_ - xb) <= 0.05 for xb in bordes_seg)
                    mod.apoyos.setdefault(n, "rotula" if borde else "x")

    # ---- barras: partidas en todos los nudos que caen sobre ellas ----
    def barra(x0, z0, x1, z1, sec, rol="viga", origen=None):
        L = math.hypot(x1 - x0, z1 - z0)
        if L < 1e-3:
            return
        ux, uz = (x1 - x0) / L, (z1 - z0) / L
        sobre = []
        for n, (x, z) in mod.nudos.items():
            t = (x - x0) * ux + (z - z0) * uz
            if 1e-3 < t < L - 1e-3 and abs((x - x0) * uz - (z - z0) * ux) < 2e-3:
                sobre.append((t, n))
        cadena = [nudo(x0, z0)] + [n for _, n in sorted(sobre)] + [nudo(x1, z1)]
        for a, b in zip(cadena, cadena[1:]):
            nombre = f"F{len(mod.barras) + 1}"
            mod.barras.append((nombre, a, b, sec))
            mod.roles[nombre] = rol
            mod.origen[nombre] = origen

    # extremos de las vigas que llegan a un pilar: al eje del pilar (como los muros)
    for v in vigas:
        for kx, kz in (("x0", "z0"), ("x1", "z1")):
            xp_ = a_pilar(v[kx])
            if xp_ != v[kx] and any(abs(a_pilar((p["x0"] + p["x1"]) / 2) - xp_) < 1e-6
                                    and min(p["z0"], p["z1"]) - tol_borde <= v[kz] <= max(p["z0"], p["z1"]) + tol_borde
                                    for p in pilares):
                v[kx] = xp_
    # primero todos los nudos de los extremos, para que cada barra se parta donde le llega otra (p. ej. un pilar donde
    # llega una viga intermedia)
    for p in pilares:
        x = a_pilar((p["x0"] + p["x1"]) / 2)
        nudo(x, p["z0"]); nudo(x, p["z1"])
    for v in vigas:
        nudo(v["x0"], v["z0"]); nudo(v["x1"], v["z1"])
    for p in pilares:
        x = a_pilar((p["x0"] + p["x1"]) / 2)
        barra(x, p["z0"], x, p["z1"], _seccion(p, mod.secciones, mod.avisos, op.get("secciones")), "pilar", str(p.get("id")))
        mod.apoyos[nudo(x, p["z0"])] = (op.get("apoyos") or {}).get(str(p.get("id")), apoyo_pilares)
    # techo: vigas inclinadas y las que no tienen otra viga encima (reciben el peso del techo, la sobrecarga y el viento)
    def encima(v):
        xm, zm = (v["x0"] + v["x1"]) / 2, (v["z0"] + v["z1"]) / 2
        for o in vigas:
            if o is v or not (min(o["x0"], o["x1"]) <= xm <= max(o["x0"], o["x1"])) or abs(o["x1"] - o["x0"]) < 1e-6:
                continue
            if o["z0"] + (o["z1"] - o["z0"]) * (xm - o["x0"]) / (o["x1"] - o["x0"]) > zm + 0.3:
                return True
        return False
    for v in vigas:
        rol = "diagonal" if v.get("tipo") == "diagonal" else (
            "techo" if abs(v["z1"] - v["z0"]) > 0.05 or not encima(v) else "viga")
        barra(v["x0"], v["z0"], v["x1"], v["z1"], _seccion(v, mod.secciones, mod.avisos, op.get("secciones")), rol, str(v.get("id")))
    mod.ancho_tributario = float(eje.get("ancho_tributario") or 0.0)
    if not mod.barras and not mod.areas:
        mod.avisos.append("El eje no tiene elementos para modelar")
    return mod


# ------------------------------------------------------------------------------------------------ archivo .s2k

DIR_S2K = {"X": "X", "Z": "Z", "GRAV": "Gravity", "GRAV_PROY": '"Gravity Projected"'}
DIR_COM = {"X": 4, "Z": 6, "GRAV": 10, "GRAV_PROY": 11}
TIPO_PATRON = {"DEAD": 1, "QUAKE": 5, "WIND": 6, "ROOF LIVE": 11}


def escribir_s2k(mod: ModeloEje, ruta: str, cargas=None) -> None:
    """Archivo de texto de SAP2000 (.s2k) para Archivo → Importar → SAP2000 .s2k. Modelo plano XZ (y = 0)."""
    L = []
    def tabla(nombre, filas):
        if filas:
            L.append(f'TABLE:  "{nombre}"')
            L.extend("   " + "   ".join(f"{k}={v}" for k, v in f.items()) for f in filas)
    def q(s):
        return f'"{s}"' if any(c in str(s) for c in " ,/") else str(s)

    tabla("PROGRAM CONTROL", [{"ProgramName": "SAP2000", "Version": "24.0.0", "CurrUnits": '"Tonf, m, C"'}])
    tabla("ACTIVE DEGREES OF FREEDOM", [{"UX": "Yes", "UY": "No", "UZ": "Yes", "RX": "No", "RY": "Yes", "RZ": "No"}])
    tabla("MATERIAL PROPERTIES 01 - GENERAL",
          [{"Material": m, "Type": d["tipo"], "SymType": "Isotropic", "TempDepend": "No"} for m, d in mod.materiales.items()])
    tabla("MATERIAL PROPERTIES 02 - BASIC MECHANICAL PROPERTIES",
          [{"Material": m, "UnitWeight": round(d["gamma"], 6), "UnitMass": round(d["gamma"] / G_ACC, 6),
            "E1": round(d["E"], 2), "G12": round(d["E"] / (2 * (1 + d["nu"])), 2), "U12": d["nu"], "A1": 1.17e-5}
           for m, d in mod.materiales.items()])
    filas = []
    for n, s in mod.secciones.items():
        if s["forma"] == "I":
            filas.append({"SectionName": q(n), "Material": s["material"], "Shape": '"I/Wide Flange"', "t3": s["d"],
                          "t2": s["bf"], "tf": s["tf"], "tw": s["tw"], "t2b": s["bf"], "tfb": s["tf"]})
        else:
            filas.append({"SectionName": q(n), "Material": s["material"], "Shape": "Box/Tube", "t3": s["d"],
                          "t2": s["bf"], "tf": s["t"], "tw": s["t"]})
    tabla("FRAME SECTION PROPERTIES 01 - GENERAL", filas)
    tabla("AREA SECTION PROPERTIES",
          [{"Section": n, "Material": a["material"], "MatAngle": 0, "AreaType": "Shell", "Type": "Membrane",
            "Thickness": round(a["espesor"], 4), "BendThick": round(a["espesor"], 4),
            "F11Mod": round(a["f11"], 6), "F22Mod": round(a["f22"], 6), "F12Mod": round(a["f12"], 6),
            "M11Mod": 1, "M22Mod": 1, "M12Mod": 1, "V13Mod": 1, "V23Mod": 1, "MMod": 1, "WMod": a["peso"]}
           for n, a in mod.secciones_area.items()])
    tabla("JOINT COORDINATES",
          [{"Joint": n, "CoordSys": "GLOBAL", "CoordType": "Cartesian", "XorR": x, "Y": 0, "Z": z, "SpecialJt": "No"}
           for n, (x, z) in mod.nudos.items()])
    tabla("JOINT RESTRAINT ASSIGNMENTS",
          [{"Joint": n, "U1": "Yes", "U2": "Yes", "U3": "No" if t == "x" else "Yes", "R1": "Yes" if t == "empotrado" else "No",
            "R2": "Yes" if t == "empotrado" else "No", "R3": "Yes" if t == "empotrado" else "No"}
           for n, t in mod.apoyos.items()])
    tabla("CONNECTIVITY - FRAME", [{"Frame": f, "JointI": a, "JointJ": b, "IsCurved": "No"} for f, a, b, _ in mod.barras])
    tabla("FRAME SECTION ASSIGNMENTS",
          [{"Frame": f, "SectionType": '"I/Wide Flange"' if mod.secciones[s]["forma"] == "I" else "Box/Tube",
            "AutoSelect": "N.A.", "AnalSect": q(s), "MatProp": "Default"} for f, _, _, s in mod.barras])
    tabla("CONNECTIVITY - AREA",
          [dict({"Area": a, "NumJoints": len(ns)}, **{f"Joint{j + 1}": n for j, n in enumerate(ns)}) for a, ns, _ in mod.areas])
    tabla("AREA SECTION ASSIGNMENTS", [{"Area": a, "Section": s, "MatProp": "Default"} for a, _, s in mod.areas])
    if cargas is None:
        tabla("LOAD PATTERN DEFINITIONS", [{"LoadPat": "DEAD", "DesignType": "Dead", "SelfWtMult": 1}])
    else:
        # el sismo (espectro NCh433/DS61) necesita el periodo del modelo: se arma con --sap; aquí van D, Lr y W
        tipos = {"DEAD": "Dead", "ROOF LIVE": '"Roof Live"', "WIND": "Wind"}
        tabla("LOAD PATTERN DEFINITIONS", [{"LoadPat": n, "DesignType": tipos[t], "SelfWtMult": 1 if n == "DEAD" else 0}
                                           for n, t in cargas.patrones.items() if t in tipos])
        tabla("FRAME LOADS - DISTRIBUTED",
              [{"Frame": f, "LoadPat": pat, "CoordSys": "GLOBAL", "Type": "Force", "Dir": DIR_S2K[d], "DistType": "RelDist",
                "RelDistA": 0, "RelDistB": 1, "FOverLA": round(v, 6), "FOverLB": round(v, 6)}
               for f, pat, d, v in cargas.distribuidas])
        filas = []
        for combo, casos in cargas.combos.items():
            if "E" in casos:
                continue
            for i, (caso, fac) in enumerate(casos.items()):
                fila = {"ComboName": q(combo), "ComboType": '"Linear Add"' if i == 0 else None, "AutoDesign": "No",
                        "CaseType": '"Linear Static"', "CaseName": caso, "ScaleFactor": fac, "SteelDesign": "Strength"}
                filas.append({k: v for k, v in fila.items() if v is not None})
        tabla("COMBINATION DEFINITIONS", filas)
    L.append("END TABLE DATA")
    with open(ruta, "w", encoding="utf-8") as f:
        f.write(f"File {ruta} -- {mod.nombre}, generado por sap2000-toolkit desde Revit (cad2revit)\n\n")
        f.write("\n".join(L) + "\n")


# ------------------------------------------------------------------------------------------------ SAP2000 (COM)

def construir_en_sap(mod: ModeloEje, sap_model) -> None:
    """Arma el modelo en un SapModel en blanco (unidades Tonf, m)."""
    from .errors import check
    from . import units

    check(sap_model.SetPresentUnits(units.TON_M_C), message="SetPresentUnits")
    tipos = {"Steel": 1, "Concrete": 2, "Other": 3}          # eMatType: 3 = NoDesign
    for m, d in mod.materiales.items():
        check(sap_model.PropMaterial.SetMaterial(m, tipos.get(d["tipo"], 6)), message=f"material {m}")
        check(sap_model.PropMaterial.SetMPIsotropic(m, d["E"], d["nu"], 1.17e-5), message=f"material {m}")
        check(sap_model.PropMaterial.SetWeightAndMass(m, 1, d["gamma"]), message=f"peso {m}")
    for n, s in mod.secciones.items():
        if s["forma"] == "I":
            check(sap_model.PropFrame.SetISection(n, s["material"], s["d"], s["bf"], s["tf"], s["tw"], s["bf"], s["tf"]),
                  message=f"sección {n}")
        else:
            check(sap_model.PropFrame.SetTube(n, s["material"], s["d"], s["bf"], s["t"], s["t"]), message=f"sección {n}")
    for n, a in mod.secciones_area.items():
        # ShellType 3 = membrana
        check(sap_model.PropArea.SetShell_1(n, 3, True, a["material"], 0, a["espesor"], a["espesor"]), message=f"área {n}")
        check(sap_model.PropArea.SetModifiers(n, [a["f11"], a["f22"], a["f12"], 1, 1, 1, 1, 1, 1, a["peso"]]),
              message=f"modificadores {n}")
    real = {}
    for n, (x, z) in mod.nudos.items():
        r = sap_model.PointObj.AddCartesian(x, 0.0, z, "", n)
        check(r, message=f"nudo {n}")
        real[n] = r[0] if isinstance(r, (list, tuple)) else n
    for n, t in mod.apoyos.items():
        fijo = t == "empotrado"
        check(sap_model.PointObj.SetRestraint(real[n], [True, True, t != "x", fijo, fijo, fijo]), message=f"apoyo {n}")
    for f, a, b, s in mod.barras:
        r = sap_model.FrameObj.AddByPoint(real[a], real[b], "", s, f)
        check(r, message=f"barra {f}")
    for a, ns, s in mod.areas:
        r = sap_model.AreaObj.AddByPoint(len(ns), [real[x] for x in ns], "", s, a)
        check(r, message=f"área {a}")
    check(sap_model.Analyze.SetActiveDOF([True, False, True, False, True, False]), message="grados de libertad XZ")


def _periodo_x(sap_model) -> float:
    """Periodo del modo con mayor masa participante en X (para R* de NCh433)."""
    r = sap_model.Results
    r.Setup.DeselectAllCasesAndCombosForOutput()
    r.Setup.SetCaseSelectedForOutput("MODAL")
    out = r.ModalParticipatingMassRatios(0, [], [], [], [], [], [], [], [], [], [], [], [], [], [], [], [])
    periodos, ux = out[4], out[5]
    i = max(range(len(ux)), key=lambda k: ux[k])
    return float(periodos[i])


def cargar_y_disenar(mod: ModeloEje, cargas, pc, sap_model, *, disenar: bool = True) -> dict:
    """Patrones, cargas, sismo (espectro NCh433/DS61 con el T* del modelo), combinaciones NCh3171, análisis y
    chequeo de los perfiles de acero con AISC 360-16 (la NCh427/1 sigue a la AISC 360)."""
    from .errors import check
    from .eje_cargas import espectro_ds61

    for n, t in cargas.patrones.items():
        if n == "DEAD":
            continue                                    # ya existe, con peso propio
        if t == "QUAKE":
            continue                                    # el sismo va como caso de espectro
        check(sap_model.LoadPatterns.Add(n, TIPO_PATRON[t], 0, True), message=f"patrón {n}")
    for f, pat, d, v in cargas.distribuidas:
        check(sap_model.FrameObj.SetLoadDistributed(f, pat, 1, DIR_COM[d], 0, 1, v, v, "Global", True, False, 0),
              message=f"carga {pat} en {f}")
    # masa sísmica: elementos + peso del techo (D); la sobrecarga de techo no se considera (NCh433)
    check(sap_model.SourceMass.SetMassSource("MSSSRC1", True, False, True, True, 1, ["DEAD"], [1.0]),
          message="fuente de masa")
    check(sap_model.LoadCases.ModalEigen.SetNumberModes("MODAL", pc.modos, 1), message="modos")
    # 1) análisis para el periodo fundamental
    check(sap_model.Analyze.RunAnalysis(), message="análisis modal")
    t_est = _periodo_x(sap_model)
    check(sap_model.SetModelIsLocked(False), message="desbloquear modelo")
    espectro = espectro_ds61(pc, t_est)
    check(sap_model.Func.FuncRS.SetUser("NCh433_DS61", len(espectro), [t for t, _ in espectro], [a for _, a in espectro], 0.05),
          message="espectro")
    rs = sap_model.LoadCases.ResponseSpectrum
    check(rs.SetCase("E"), message="caso E")
    check(rs.SetModalCase("E", "MODAL"), message="caso E")
    check(rs.SetLoads("E", 1, ["U1"], ["NCh433_DS61"], [G_ACC], ["Global"], [0.0]), message="caso E")
    for combo, casos in cargas.combos.items():
        check(sap_model.RespCombo.Add(combo, 0), message=f"combinación {combo}")
        for caso, fac in casos.items():
            check(sap_model.RespCombo.SetCaseList(combo, 0, caso, fac), message=f"combinación {combo}")
    # 2) análisis completo y diseño
    check(sap_model.Analyze.RunAnalysis(), message="análisis")
    res = {"T*": t_est, "espectro": espectro, "razones": []}
    if disenar and mod.secciones:
        ds = sap_model.DesignSteel
        check(ds.SetCode("AISC 360-16"), message="norma de diseño AISC 360-16")
        for combo in cargas.combos:
            check(ds.SetComboStrength(combo, True), message=f"combinación de diseño {combo}")
        check(ds.StartDesign(), message="diseño de acero")
        out = ds.GetSummaryResults("ALL", 0, [], [], [], [], [], [], [], 1)
        n, nombres, razones, tipos, ubic, combos = out[0], out[1], out[2], out[3], out[4], out[5]
        res["razones"] = sorted(zip(nombres, razones, combos), key=lambda x: -x[1])
    return res


# ------------------------------------------------------------------------------------------------ CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="Modelo SAP2000 de un eje exportado desde Revit (cad2revit)")
    ap.add_argument("eje", help="archivo eje-*.sap.json")
    ap.add_argument("--s2k", help="escribe el modelo como .s2k (Archivo → Importar en SAP2000)")
    ap.add_argument("--sap", action="store_true", help="arma el modelo en SAP2000 (Windows, vía COM)")
    ap.add_argument("--guardar", help="ruta .sdb donde guardar el modelo de SAP2000")
    ap.add_argument("--malla", type=float, default=0.5, help="tamaño máximo de los shells (m)")
    ap.add_argument("--empotrar-pilares", action="store_true", help="bases de pilares empotradas (por defecto rotuladas)")
    ap.add_argument("--clavos", type=int, default=100, help="espaciamiento del clavado perimetral (mm): 150, 100 o 50")
    ap.add_argument("--tableros", type=int, default=1, help="caras con OSB (1 o 2)")
    ap.add_argument("--ga", type=float, help="rigidez de corte aparente Ga (tonf/m), en vez de la tabla")
    ap.add_argument("--k-anclaje", type=float, help="rigidez del anclaje (tonf/m)")
    ap.add_argument("--sin-cargas", action="store_true", help="solo geometría (sin cargas ni combinaciones)")
    ap.add_argument("--peso-techo", type=float, default=30, help="peso del techo (kgf/m²)")
    ap.add_argument("--ancho", type=float, help="ancho colaborante (m) para pasar cargas por m² a cargas por m; "
                                                "por defecto, el ancho tributario del eje exportado por Revit")
    ap.add_argument("--viento", type=float, default=40, help="velocidad básica del viento V (m/s, NCh432; 40 = Ancud, Tabla 6)")
    ap.add_argument("--exposicion", default="C", choices=["B", "C", "D"])
    ap.add_argument("--ancho-edificio", type=float, help="B: dimensión del edificio normal al viento (m), para el Cp "
                                                          "del muro a sotavento (NCh432 Tabla 14)")
    ap.add_argument("--zona", type=int, default=2, choices=[1, 2, 3], help="zona sísmica (NCh433)")
    ap.add_argument("--suelo", default="C", choices=list("ABCDE"), help="tipo de suelo (DS61)")
    ap.add_argument("--importancia", type=float, default=1.0, help="coeficiente de importancia sísmico I")
    ap.add_argument("--r0", type=float, default=None, help="R0 de NCh433:2026 (vacío = según el sistema, Tabla 5)")
    ap.add_argument("--sin-diseno", action="store_true", help="no correr el chequeo de acero AISC 360-16")
    ap.add_argument("--sin-reduccion-lr", action="store_true", help="sobrecarga de techo Lo sin la reducción de NCh1537 §8.2")
    a = ap.parse_args(argv)
    with open(a.eje, encoding="utf-8") as f:
        eje = json.load(f)
    if eje.get("tipo_archivo") != "cad2revit-eje-sap":
        raise SystemExit("No es un archivo .sap.json del botón «Eje a SAP2000» de cad2revit")
    pm = ParametrosMuroMadera(espaciamiento_clavos=a.clavos, tableros=a.tableros, ga=a.ga)
    if a.k_anclaje:
        pm.k_anclaje = a.k_anclaje
    mod = mallar(eje, pm, malla=a.malla, apoyo_pilares="empotrado" if a.empotrar_pilares else "rotula")
    print(f"{mod.nombre}: {len(mod.nudos)} nudos, {len(mod.barras)} barras, {len(mod.areas)} shells")
    for r in mod.resumen_muros:
        print(f"  muro {r['muro']} x={r['x0']}..{r['x1']} L={r['L']} H={r['H']}: K_eq={r['K_eq_tonf_m']} tonf/m "
              f"→ shell E={r['E_shell']} G={r['G_shell']} tonf/m²")
    for x in mod.avisos:
        print("  aviso:", x)
    cargas = pc = None
    if not a.sin_cargas:
        from .eje_cargas import ParametrosCargas, cargas_eje
        pc = ParametrosCargas(peso_techo=a.peso_techo / 1000, v_viento=a.viento, exposicion=a.exposicion, zona=a.zona,
                              suelo=a.suelo, importancia_sismo=a.importancia, r0=a.r0,
                              reducir_lr=not a.sin_reduccion_lr, ancho_colaborante=a.ancho,
                              ancho_edificio=a.ancho_edificio)
        cargas = cargas_eje(mod, pc)
        for x in cargas.resumen:
            print(" ", x)
        print(f"  {len(cargas.combos)} combinaciones NCh3171 (resistencia)")
    if a.s2k:
        escribir_s2k(mod, a.s2k, cargas)
        print("Escrito", a.s2k, "(sin el sismo: el espectro NCh433 se arma con --sap)" if cargas else "")
    if a.sap:
        from .connection import Sap2000Session
        from . import units
        sap = Sap2000Session()
        sap.start()
        sap.new_blank_model(units=units.TON_M_C)
        construir_en_sap(mod, sap.sap_model)
        if cargas:
            res = cargar_y_disenar(mod, cargas, pc, sap.sap_model, disenar=not a.sin_diseno)
            print(f"  T* = {res['T*']:.3f} s (modo con mayor masa en X)")
            for f, r, combo in res["razones"][:15]:
                print(f"  {f}: razón AISC 360-16 = {r:.2f} ({combo})" + ("  ← NO CUMPLE" if r > 1 else ""))
        if a.guardar:
            sap.save(a.guardar)
            print("Guardado", a.guardar)


if __name__ == "__main__":
    main()
