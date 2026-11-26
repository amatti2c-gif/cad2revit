"""Fundación 3D: todas las fundaciones del modelo 3D (corridas, aisladas, vigas) como un emparrillado de vigas
horizontales unidas en sus cruces, sobre suelo de Winkler que solo trabaja en compresión. Cada nudo tiene 3 grados
de libertad: asentamiento w (+ arriba) y giros θx, θy. Cada tramo tiene flexión en su plano vertical (E·I de la
sección pedestal + zapata) y torsión (G·J); el suelo da un resorte vertical ks·B·trib y uno al giro transversal
ks·B³/12·trib (la zapata resiste el vuelco en su ancho). Las cargas son las 6 componentes de reacción de cada apoyo
del modelo 3D: Rz, los momentos y las fuerzas horizontales aplicadas a la altura del pedestal + zapata sobre el sello.

Resultados con el mismo formato que el análisis por eje (fundacion.analizar_fundacion), una entrada por fundación, más
el resumen del conjunto: deslizamiento con la fuerza horizontal total y el roce de todas las fundaciones.
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np
import scipy.sparse as sps
import scipy.sparse.linalg as sla

from .fundacion import ASD, G_ACC, ParametrosFundacion, _expandir, disenar_seccion


def _seccion(pf: ParametrosFundacion) -> dict:
    bp, hp, B, hz = pf.b_pedestal, pf.h_pedestal, pf.b_zapata, pf.h_zapata
    A1, A2 = bp * hp, B * hz
    yc = (A1 * hp / 2 + A2 * (hp + hz / 2)) / (A1 + A2)
    I = bp * hp ** 3 / 12 + A1 * (yc - hp / 2) ** 2 + B * hz ** 3 / 12 + A2 * (hp + hz / 2 - yc) ** 2

    def j_rect(a, b):                                     # Saint-Venant de un rectángulo a × b
        a, b = max(a, b), min(a, b)
        return a * b ** 3 * (1 / 3 - 0.21 * b / a * (1 - b ** 4 / (12 * a ** 4))) if b > 0 else 0.0
    J = j_rect(bp, hp) + j_rect(B, hz)
    Ec = 4700 * math.sqrt(pf.fc) * 1000 / G_ACC
    peso = pf.gamma_hormigon * (A1 + A2) + pf.gamma_suelo * max(B - bp, 0) * max(pf.profundidad - hz, 0)
    return {"I": I, "J": J, "EI": Ec * I, "GJ": Ec / (2 * 1.2) * J, "yc": yc, "Ht": hp + hz, "peso": peso,
            "kv": pf.ks * 1000 * B, "kr": pf.ks * 1000 * B ** 3 / 12}


def analizar_emparrillado(lineas: list[dict], combos_lrfd: dict, patrones: dict, pf_base: ParametrosFundacion,
                          avance=None) -> dict:
    """lineas: [{nombre, dir (ux, uy), A (x, y) o eje/coord, x0, x1, geometria, apoyos [{nudo, t, casos 6 comp.}]}]."""
    def paso(pct, etapa, hasta=None):
        if avance:
            avance(round(pct), etapa, round(hasta if hasta is not None else pct))
    paso(2, "Armando el emparrillado de fundaciones")
    L_ = []
    for l_ in lineas:
        u = np.array(l_.get("dir") or (1.0, 0.0), float)
        u /= np.linalg.norm(u)
        A = np.array(l_["A"], float) if l_.get("A") is not None else (np.array([0.0, l_["coord"]]) if l_["eje"] == "X" else np.array([l_["coord"], 0.0]))
        ts = [a["t"] for a in l_["apoyos"]]
        pf = dataclasses.replace(pf_base, **(l_.get("geometria") or {}))
        x0 = l_.get("x0") if l_.get("x0") is not None else min(ts) - pf.voladizo
        x1 = l_.get("x1") if l_.get("x1") is not None else max(ts) + pf.voladizo
        if ts:
            x0, x1 = min(x0, min(ts) - 0.05), max(x1, max(ts) + 0.05)
        L_.append({"l": l_, "u": u, "n": np.array([-u[1], u[0]]), "A": A, "x0": x0, "x1": x1, "pf": pf, "sec": _seccion(pf),
                   "puntos": set(ts)})
    # cruces y encuentros (T, L, +): un nudo común; los extremos que llegan a otra fundación a menos de su medio ancho
    # se unen a ella
    uniones = []
    for i in range(len(L_)):
        for j in range(i + 1, len(L_)):
            a, b = L_[i], L_[j]
            M = np.array([a["u"], -b["u"]]).T
            if abs(np.linalg.det(M)) < 1e-6:
                continue
            s, r = np.linalg.solve(M, b["A"] - a["A"])
            tol_a = b["pf"].b_zapata / 2 + 0.05
            tol_b = a["pf"].b_zapata / 2 + 0.05
            if a["x0"] - tol_a <= s <= a["x1"] + tol_a and b["x0"] - tol_b <= r <= b["x1"] + tol_b:
                s, r = min(max(s, a["x0"]), a["x1"]), min(max(r, b["x0"]), b["x1"])
                a["puntos"].add(float(s))
                b["puntos"].add(float(r))
                uniones.append((i, float(s), j, float(r)))
    # nudos de cada fundación: malla de pf.dx más los apoyos y los cruces
    gid = {}
    coords = []
    for i, e in enumerate(L_):
        n_el = max(int(math.ceil((e["x1"] - e["x0"]) / e["pf"].dx)), 2)
        malla = np.linspace(e["x0"], e["x1"], n_el + 1)
        paso_m = (e["x1"] - e["x0"]) / n_el
        esp = sorted(e["puntos"])
        malla = [x for x in malla if all(abs(x - p) > 0.25 * paso_m for p in esp)]
        e["t"] = np.array(sorted(set(np.round(np.concatenate([malla, esp]), 6))))
        e["ids"] = []
        for t in e["t"]:
            k = len(coords)
            coords.append(e["A"] + e["u"] * t)
            e["ids"].append(k)
            gid[(i, round(float(t), 6))] = k
    # unir los nudos de los cruces
    padre = list(range(len(coords)))

    def raiz(k):
        while padre[k] != k:
            padre[k] = padre[padre[k]]
            k = padre[k]
        return k
    for i, s, j, r in uniones:
        ka = L_[i]["ids"][int(np.argmin(np.abs(L_[i]["t"] - s)))]
        kb = L_[j]["ids"][int(np.argmin(np.abs(L_[j]["t"] - r)))]
        padre[raiz(kb)] = raiz(ka)
    unicos = sorted({raiz(k) for k in range(len(coords))})
    num = {k: n for n, k in enumerate(unicos)}
    for e in L_:
        e["ids"] = [num[raiz(k)] for k in e["ids"]]
    nn = len(unicos)
    ndof = 3 * nn
    # rigidez de las vigas y resortes del suelo
    filas, cols, vals = [], [], []
    kv_n = np.zeros(nn)
    kr_n = {}                                              # nudo → [(kr, ux, uy)]
    peso_n = np.zeros(nn)
    for e in L_:
        ux, uy = e["u"]
        sec = e["sec"]
        t = e["t"]
        trib = np.zeros(len(t))
        trib[:-1] += np.diff(t) / 2
        trib[1:] += np.diff(t) / 2
        e["trib"] = trib
        for k, nid in enumerate(e["ids"]):
            kv_n[nid] += sec["kv"] * trib[k]
            kr_n.setdefault(nid, []).append((sec["kr"] * trib[k], ux, uy))
            peso_n[nid] += sec["peso"] * trib[k]
        for k in range(len(t) - 1):
            L = t[k + 1] - t[k]
            if L < 1e-6:
                continue
            EI, GJ = sec["EI"], sec["GJ"]
            kb = EI / L ** 3 * np.array([[12, 6 * L, -12, 6 * L], [6 * L, 4 * L * L, -6 * L, 2 * L * L],
                                         [-12, -6 * L, 12, -6 * L], [6 * L, 2 * L * L, -6 * L, 4 * L * L]])
            Tb = np.zeros((4, 6))                          # (w, φ) por nudo; φ = dw/ds = uy·θx − ux·θy
            Tb[0, 0] = Tb[2, 3] = 1
            Tb[1, 1], Tb[1, 2], Tb[3, 4], Tb[3, 5] = uy, -ux, uy, -ux
            Tt = np.zeros((2, 6))                          # torsión: θt = ux·θx + uy·θy
            Tt[0, 1], Tt[0, 2], Tt[1, 4], Tt[1, 5] = ux, uy, ux, uy
            ke = Tb.T @ kb @ Tb + Tt.T @ (GJ / L * np.array([[1, -1], [-1, 1]])) @ Tt
            dofs = [3 * e["ids"][k] + q for q in range(3)] + [3 * e["ids"][k + 1] + q for q in range(3)]
            for a_ in range(6):
                for b_ in range(6):
                    if ke[a_, b_]:
                        filas.append(dofs[a_]); cols.append(dofs[b_]); vals.append(ke[a_, b_])
    K_vigas = sps.csr_matrix((vals, (filas, cols)), shape=(ndof, ndof))
    # resortes al giro transversal (en coordenadas globales): kr·n⊗n en (θx, θy), con n = eje de la fundación
    Kr = sps.lil_matrix((ndof, ndof))
    for nid, lst in kr_n.items():
        for kr, ux, uy in lst:
            Kr[3 * nid + 1, 3 * nid + 1] += kr * ux * ux
            Kr[3 * nid + 1, 3 * nid + 2] += kr * ux * uy
            Kr[3 * nid + 2, 3 * nid + 1] += kr * ux * uy
            Kr[3 * nid + 2, 3 * nid + 2] += kr * uy * uy
    Kr = Kr.tocsr()
    # apoyos → nudos
    for i, e in enumerate(L_):
        Ht = e["sec"]["Ht"]
        for a in e["l"]["apoyos"]:
            a["_nid"] = e["ids"][int(np.argmin(np.abs(e["t"] - a["t"])))]
            a["_Ht"] = Ht
    vientos = sorted({c for e in L_ for a in e["l"]["apoyos"] for c in a["casos"] if patrones.get(c) == "WIND"})

    def cargas(fac):
        F = np.zeros(ndof)
        F[0::3] = -fac.get("DEAD", 0.0) * peso_n
        H = np.zeros(2)
        for e in L_:
            for a in e["l"]["apoyos"]:
                r = np.zeros(6)
                for c, f in fac.items():
                    v = a["casos"].get(c)
                    if v is not None:
                        r += f * np.array(v)
                nid, Ht = a["_nid"], a["_Ht"]
                F[3 * nid] += -r[2]
                F[3 * nid + 1] += -r[3] + Ht * r[1]             # fuerza −Ry a Ht sobre el sello → Mx = Ht·Ry
                F[3 * nid + 2] += -r[4] - Ht * r[0]             # fuerza −Rx a Ht → My = −Ht·Rx
                H += -r[:2]
        return F, H

    def resolver(F):
        activo = np.ones(nn, bool)
        for _ in range(40):
            kv = np.where(activo, kv_n, 0.0)
            Ks = sps.diags(np.repeat(kv, 3) * np.tile([1.0, 0, 0], nn))
            act3 = np.repeat(activo, 3).astype(float)
            K = K_vigas + Ks + sps.diags(act3) @ Kr @ sps.diags(act3)
            try:
                with np.errstate(all="raise"):
                    d = sla.spsolve(K.tocsc(), F)
            except Exception:                                        # noqa: BLE001
                return None
            if not np.all(np.isfinite(d)):
                return None
            w = d[0::3]
            nuevo = w < 0                                     # compresión: el suelo empuja hacia arriba
            if np.array_equal(nuevo, activo):
                break
            activo = nuevo
            if not activo.any():
                return None
        # equilibrio vertical: la suma de las reacciones del suelo debe igualar la carga
        qv = np.where(activo, -kv_n * d[0::3], 0.0)
        if abs(qv.sum() - (-F[0::3].sum())) > 1e-3 * max(abs(F[0::3]).sum(), 1.0):
            return None
        return {"d": d, "activo": activo, "qv": qv}

    def esfuerzos(e, d):
        """Momento flector (+ tracción abajo), corte y torsión en los nudos de la fundación e."""
        ux, uy = e["u"]
        t = e["t"]
        M = []
        V = [0.0]
        T = [0.0]
        EI, GJ = e["sec"]["EI"], e["sec"]["GJ"]
        for k in range(len(t) - 1):
            L = t[k + 1] - t[k]
            if L < 1e-6:
                continue
            i1, i2 = e["ids"][k], e["ids"][k + 1]
            w1, w2 = d[3 * i1], d[3 * i2]
            f1 = uy * d[3 * i1 + 1] - ux * d[3 * i1 + 2]
            f2 = uy * d[3 * i2 + 1] - ux * d[3 * i2 + 2]
            kb = EI / L ** 3 * np.array([[12, 6 * L, -12, 6 * L], [6 * L, 4 * L * L, -6 * L, 2 * L * L],
                                         [-12, -6 * L, 12, -6 * L], [6 * L, 2 * L * L, -6 * L, 4 * L * L]])
            q = kb @ np.array([w1, f1, w2, f2])                # fuerzas en los extremos (V1, M1, V2, M2)
            M += [-q[1], q[3]]                                  # flector interno (+ tracción abajo) en los extremos
            V.append(abs(q[0]))
            tt = GJ / L * ((ux * d[3 * i2 + 1] + uy * d[3 * i2 + 2]) - (ux * d[3 * i1 + 1] + uy * d[3 * i1 + 2]))
            T.append(abs(tt))
        return np.array(M or [0.0]), np.array(V), np.array(T)

    # ---- servicio ----
    q_ev = pf_base.q_adm_eventual or 1.33 * pf_base.q_adm
    lista_srv = _expandir(ASD, vientos)
    srv = {i: [] for i in range(len(L_))}
    glob_srv = []
    for k_s, (nombre, fac, ev) in enumerate(lista_srv):
        paso(5 + 50 * k_s / len(lista_srv), f"Suelo: combinación de servicio {k_s + 1} de {len(lista_srv)}")
        F, H = cargas(fac)
        r = resolver(F)
        if r is None:
            for i in srv:
                srv[i].append({"combo": nombre, "estable": False, "eventual": ev})
            glob_srv.append({"combo": nombre, "estable": False})
            continue
        d, act, qv = r["d"], r["activo"], r["qv"]
        N_tot = float(qv.sum())
        H_tot = float(np.linalg.norm(H))
        fs = pf_base.roce * N_tot / H_tot if H_tot > 1e-9 else None
        glob_srv.append({"combo": nombre, "estable": True, "N": N_tot, "H": H_tot, "FS_deslizamiento": fs})
        for i, e in enumerate(L_):
            B = e["pf"].b_zapata
            ids = e["ids"]
            q_med = np.array([max(-d[3 * n], 0.0) * pf_base.ks * 1000 if act[n] else 0.0 for n in ids])   # tonf/m², media en el ancho
            th = np.array([e["u"][0] * d[3 * n + 1] + e["u"][1] * d[3 * n + 2] for n in ids])
            q_borde = np.where(act[ids], q_med + pf_base.ks * 1000 * B / 2 * np.abs(th), 0.0)   # + vuelco transversal
            largo_c = float(np.sum(e["trib"][act[ids]]))
            Ln = e["x1"] - e["x0"]
            qmax = float(np.max(q_borde))
            srv[i].append({"combo": nombre, "estable": True, "eventual": ev, "q_max": qmax,
                           "q_adm": q_ev if ev else pf_base.q_adm, "FU_presion": qmax / (q_ev if ev else pf_base.q_adm),
                           "pct_compresion": largo_c / Ln, "N": float(np.sum(q_med * B * e["trib"])), "H": H_tot,
                           "FS_deslizamiento": fs, "q": [float(v) for v in q_med],
                           "asentamiento_max_mm": float(-np.min(d[[3 * n for n in ids]]) * 1000)})
    # ---- resistencia ----
    lista_lrfd = _expandir(combos_lrfd, vientos)
    peor = {i: {"Mpos": (0.0, ""), "Mneg": (0.0, ""), "V": (0.0, ""), "qu": (0.0, ""), "H": (0.0, ""), "P_apoyo": {}, "T": (0.0, "")}
            for i in range(len(L_))}
    for k_l, (nombre, fac, _) in enumerate(lista_lrfd):
        paso(55 + 40 * k_l / len(lista_lrfd), f"Hormigón: combinación mayorada {k_l + 1} de {len(lista_lrfd)}")
        F, H = cargas(fac)
        r = resolver(F)
        if r is None:
            continue
        d, act, qv = r["d"], r["activo"], r["qv"]
        for i, e in enumerate(L_):
            pz = peor[i]
            M, V, T = esfuerzos(e, d)
            B = e["pf"].b_zapata
            th = np.array([e["u"][0] * d[3 * n + 1] + e["u"][1] * d[3 * n + 2] for n in e["ids"]])
            q_med = np.array([max(-d[3 * n], 0) * pf_base.ks * 1000 for n in e["ids"]])
            qu = float(np.max(q_med + pf_base.ks * 1000 * B / 2 * np.abs(th) * act[e["ids"]]))
            Hl = sum(abs(sum(f * a["casos"][c][0] * e["u"][0] + f * a["casos"][c][1] * e["u"][1]
                             for c, f in fac.items() if c in a["casos"])) for a in e["l"]["apoyos"])
            for clave, val in (("Mpos", float(np.max(M))), ("Mneg", float(-np.min(M))), ("V", float(np.max(V))), ("qu", qu),
                               ("H", Hl), ("T", float(np.max(T)))):
                if val > pz[clave][0]:
                    pz[clave] = (val, nombre)
            for a in e["l"]["apoyos"]:
                rz = sum(f * a["casos"][c][2] for c, f in fac.items() if c in a["casos"])
                if rz > pz["P_apoyo"].get(a["nudo"], (0.0, ""))[0]:
                    pz["P_apoyo"][a["nudo"]] = (rz, nombre)
    paso(96, "Diseño de las zapatas y pedestales")
    salida = []
    ok_g = [g for g in glob_srv if g["estable"]]
    fs_glob = min((g["FS_deslizamiento"] for g in ok_g if g["FS_deslizamiento"]), default=None)
    for i, e in enumerate(L_):
        pf = e["pf"]
        apoyos = [{"x": a["t"], "nudo": a["nudo"], "pilar": a.get("pilar"), "muro": a.get("muro"), "tipo": a.get("tipo")}
                  for a in e["l"]["apoyos"]]
        Ln = e["x1"] - e["x0"]
        viga, zapata, muro = disenar_seccion(pf, Ln, peor[i], apoyos)
        if peor[i]["T"][0] > 1e-6:
            viga.append({"verificacion": "Torsión de la viga de fundación (referencia; emparrillado 3D)", "Vu_tonf": None,
                         "Mu_tonfm": peor[i]["T"][0], "combo": peor[i]["T"][1], "phiMn_tonfm": None, "razon": None,
                         "notas": ["Tu máximo por el giro transversal y los cruces; verificar según ACI 318-19 cap. 9.5.4 si es relevante"]})
        ok = [s for s in srv[i] if s["estable"]]
        sec = e["sec"]
        salida.append({
            "x": [float(v) for v in e["t"]], "L": Ln, "x0": e["x0"], "x1": e["x1"], "EI": sec["EI"], "peso_propio_tonf_m": sec["peso"],
            "seccion": {"b_pedestal": pf.b_pedestal, "h_pedestal": pf.h_pedestal, "b_zapata": pf.b_zapata, "h_zapata": pf.h_zapata,
                        "I_m4": sec["I"], "J_m4": sec["J"], "yc_m": sec["yc"], "profundidad": pf.profundidad},
            "apoyos": apoyos, "servicio": srv[i],
            "resumen_suelo": {
                "pct_compresion_min": min((s["pct_compresion"] for s in ok), default=0.0),
                "combo_pct": min(ok, key=lambda s: s["pct_compresion"])["combo"] if ok else "",
                "FU_presion_max": max((s["FU_presion"] for s in ok), default=0.0),
                "combo_presion": max(ok, key=lambda s: s["FU_presion"])["combo"] if ok else "",
                "FS_desliz_min": fs_glob, "inestables": [s["combo"] for s in srv[i] if not s["estable"]],
                "q_adm": pf_base.q_adm, "q_adm_eventual": q_ev, "fs_desliz_exigido": pf_base.fs_deslizamiento,
                "nota_deslizamiento": "Emparrillado 3D: el deslizamiento se verifica para el conjunto de las fundaciones (fuerza "
                                      "horizontal total contra el roce de todas), sin el empuje pasivo del suelo",
            },
            "viga": viga, "zapata": zapata, "pedestal": muro,
        })
    return {"lineas": salida, "conjunto": {"servicio": glob_srv, "FS_desliz_min": fs_glob, "nudos": nn, "cruces": len(uniones)}}
