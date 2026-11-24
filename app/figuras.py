"""Figuras para la memoria de cálculo, dibujadas con matplotlib a partir de los resultados guardados en la app
(sin depender del navegador): modelo del eje, diseño (razones), modo fundamental, corte y presiones de la fundación,
planta de costaneras y momentos de la costanera más solicitada. Cada función devuelve un PNG (bytes)."""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.collections import LineCollection, PolyCollection  # noqa: E402
from matplotlib.patches import Circle, Polygon, Rectangle  # noqa: E402

plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 8})
COL_ROL = {"pilar": "#1f4e9c", "viga": "#555555", "techo": "#c06000", "diagonal": "#7a3fb0"}


def _png(fig) -> bytes:
    b = io.BytesIO()
    fig.savefig(b, format="png", dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return b.getvalue()


def _color_razon(r):
    if r is None:
        return "#1f4e9c"
    return "#b00020" if r > 1 else "#e0a000" if r > 0.7 else "#1b6e20"


def _base_eje(v: dict, ax, color_barra=None, ancho=2.0, muros_color="#e9d7b8"):
    nud = v["nudos"]
    quads = [[nud[n] for n in q[1]] for q in v.get("quads") or []]
    if quads:
        ax.add_collection(PolyCollection(quads, facecolors=muros_color, edgecolors="#c9a870", linewidths=0.3))
    segs, cols = [], []
    for b in v.get("barras") or []:
        segs.append([nud[b[1]], nud[b[2]]])
        cols.append(color_barra(b) if color_barra else COL_ROL.get(b[4], "#555555"))
    ax.add_collection(LineCollection(segs, colors=cols, linewidths=ancho))
    xs = [p[0] for p in nud.values()]
    zs = [p[1] for p in nud.values()]
    h = max(max(zs) - min(zs), max(xs) - min(xs), 1.0)
    for n, tipo in (v.get("apoyos") or {}).items():
        x, z = nud[n]
        s = 0.025 * h
        if tipo == "x":
            ax.add_patch(Circle((x, z - s * 0.6), s * 0.5, fc="white", ec="#333", lw=0.8))
        else:
            ax.add_patch(Polygon([(x, z), (x - s, z - 1.6 * s), (x + s, z - 1.6 * s)], closed=True, fc="#333", ec="#333"))
    ax.set_xlim(min(xs) - 0.05 * h, max(xs) + 0.05 * h)
    ax.set_ylim(min(zs) - 0.08 * h, max(zs) + 0.05 * h)
    ax.set_aspect("equal")
    ax.set_xlabel("x (m)")
    ax.set_ylabel("z (m)")
    ax.grid(alpha=0.25, lw=0.4)
    return h


def modelo_eje(res: dict, titulo: str = "") -> bytes:
    """Modelo computacional: barras por rol, muros como lámina (malla) y apoyos."""
    v = res["visor"]
    fig, ax = plt.subplots(figsize=(8, 4.2))
    _base_eje(v, ax)
    # secciones de cada elemento (una etiqueta por elemento de Revit)
    vistos = set()
    nud = v["nudos"]
    for b in v.get("barras") or []:
        if b[3] in vistos:
            continue
        vistos.add(b[3])
        (x0, z0), (x1, z1) = nud[b[1]], nud[b[2]]
        ax.annotate(b[5], ((x0 + x1) / 2, (z0 + z1) / 2), fontsize=6, color=COL_ROL.get(b[4], "#555"),
                    xytext=(3, 3), textcoords="offset points")
    from matplotlib.lines import Line2D
    ley = [Line2D([0], [0], color=c, lw=2, label=r) for r, c in COL_ROL.items() if any(b[4] == r for b in v["barras"])]
    ley.append(Rectangle((0, 0), 1, 1, fc="#e9d7b8", ec="#c9a870", label="muro (lámina)"))
    ax.legend(handles=ley, loc="upper right", fontsize=7, framealpha=0.9)
    if titulo:
        ax.set_title(titulo)
    return _png(fig)


def diseno_eje(res: dict, titulo: str = "") -> bytes:
    """Razones de diseño: barras de acero coloreadas por su razón (AISC) y muros por su FU."""
    v = res["visor"]
    razon = {}
    for a in res.get("acero") or []:
        for b in a.get("barras") or []:
            razon[b] = a.get("razon")
    fig, ax = plt.subplots(figsize=(8, 4.2))
    _base_eje(v, ax, color_barra=lambda b: _color_razon(razon.get(b[0])), ancho=2.6, muros_color="#efe6d6")
    nud = v["nudos"]
    vistos = set()
    for b in v.get("barras") or []:
        if b[0] in razon and b[3] not in vistos:
            vistos.add(b[3])
            (x0, z0), (x1, z1) = nud[b[1]], nud[b[2]]
            ax.annotate(f"{razon[b[0]]:.2f}", ((x0 + x1) / 2, (z0 + z1) / 2), fontsize=7, fontweight="bold",
                        color=_color_razon(razon[b[0]]), xytext=(3, -9), textcoords="offset points")
    for i, m in enumerate(sorted(res.get("madera") or [], key=lambda m: m["x0"])):
        ax.annotate(f"muro\nFU {m['FU']:.2f}", ((m["x0"] + m["x1"]) / 2, 0.45 + 0.75 * (i % 3)), fontsize=6.5, ha="center",
                    color=_color_razon(m["FU"]), bbox={"boxstyle": "round,pad=0.15", "fc": "white", "ec": "none", "alpha": 0.8})
    from matplotlib.lines import Line2D
    ax.legend(handles=[Line2D([0], [0], color=c, lw=2.6, label=t) for t, c in
                       (("razón ≤ 0,7", "#1b6e20"), ("≤ 1,0", "#e0a000"), ("> 1,0", "#b00020"))], loc="upper right", fontsize=7)
    if titulo:
        ax.set_title(titulo)
    return _png(fig)


def modo_eje(res: dict, n: int = 1, titulo: str = "") -> bytes:
    """Forma del modo n sobre el modelo (escala gráfica)."""
    v = res["visor"]
    modos = v.get("modos") or []
    m = next((x for x in modos if x.get("n") == n), modos[0] if modos else None)
    fig, ax = plt.subplots(figsize=(8, 4.2))
    h = _base_eje(v, ax, color_barra=lambda b: "#c8c8c8", ancho=1.2, muros_color="#f4f4f4")
    if m:
        nud, forma = v["nudos"], m["forma"]
        amp = max((abs(a) for p in forma.values() for a in p), default=1.0) or 1.0
        k = 0.12 * h / amp
        def d(nn):
            p, f = nud[nn], forma.get(nn, (0, 0))
            return (p[0] + k * f[0], p[1] + k * f[1])
        segs = [[d(b[1]), d(b[2])] for b in v["barras"]]
        ax.add_collection(LineCollection(segs, colors="#b00020", linewidths=1.8))
        quads = [[d(x) for x in q[1]] for q in v.get("quads") or []]
        if quads:
            ax.add_collection(PolyCollection(quads, facecolors="none", edgecolors="#e08080", linewidths=0.3))
        ax.set_title(titulo or f"Modo {m['n']}: T = {m['T']:.3f} s; masa X {m['masa_x'] * 100:.1f} %, Z {m['masa_z'] * 100:.1f} %")
    return _png(fig)


def corte_fundacion(fp: dict, titulo: str = "") -> bytes:
    """Corte del sobrecimiento (pedestal + zapata) a escala, con sus armaduras."""
    a = fp.get("armaduras") or {}
    bp, hp = fp.get("b_pedestal", 0.2) * 100, fp.get("h_pedestal", 0.6) * 100
    B, hz = fp.get("b_zapata", 0.6) * 100, fp.get("h_zapata", 0.25) * 100
    Df, rec = fp.get("profundidad", 0.8) * 100, fp.get("recubrimiento", 0.05) * 100
    n_c = int(a.get("ped_barras_por_cara", 3))
    dP, eP, sP = a.get("ped_diametro_mm", 10) / 10, a.get("ped_estribo_diametro_mm", 8) / 10, a.get("ped_estribo_separacion_cm", 20)
    nS, nI = int(a.get("fun_barras_superiores", 4)), int(a.get("fun_barras_inferiores", 4))
    dF, eF, sF = a.get("fun_diametro_mm", 10) / 10, a.get("fun_estribo_diametro_mm", 8) / 10, a.get("fun_estribo_separacion_cm", 20)
    fondo = a.get("ped_estribo_hasta_fondo", True)
    Ht = hp + hz
    fig, ax = plt.subplots(figsize=(6, 5))
    # y hacia arriba desde el sello
    ax.add_patch(Polygon([(-bp / 2, Ht), (bp / 2, Ht), (bp / 2, hz), (B / 2, hz), (B / 2, 0), (-B / 2, 0), (-B / 2, hz), (-bp / 2, hz)],
                         closed=True, fc="#dcdcdc", ec="#555", lw=1))
    ax.axhline(Df, color="#7a5c2e", ls="--", lw=0.8)
    ax.text(-B / 2 - 2, Df + 1, "N.T.", color="#7a5c2e", ha="right")
    # estribos
    ax.add_patch(Rectangle((-B / 2 + rec, rec), B - 2 * rec, hz - 2 * rec, fc="none", ec="#c0392b", lw=1.4))
    ax.add_patch(Rectangle((-bp / 2 + rec, (rec if fondo else hz - rec)), bp - 2 * rec, Ht - rec - (rec if fondo else hz - rec),
                           fc="none", ec="#d35400", lw=1.4))
    # barras
    for i in range(n_c):
        y = Ht - rec - eP - dP / 2 - (Ht - hz - 2 * rec) * (i / max(n_c - 1, 1) if n_c > 1 else 0)
        for x in (-bp / 2 + rec + eP + dP / 2, bp / 2 - rec - eP - dP / 2):
            ax.add_patch(Circle((x, y), max(dP / 2, 0.6), fc="#1f4e9c"))
    for n, y in ((nS, hz - rec - eF - dF / 2), (nI, rec + eF + dF / 2)):
        xa, xb = -B / 2 + rec + eF + dF / 2, B / 2 - rec - eF - dF / 2
        for i in range(n):
            ax.add_patch(Circle((xa + (xb - xa) * i / max(n - 1, 1) if n > 1 else 0, y), max(dF / 2, 0.6), fc="#1f4e9c"))
    # cotas y leyendas
    ax.annotate("", (-B / 2, -6), (B / 2, -6), arrowprops={"arrowstyle": "<->", "lw": 0.7})
    ax.text(0, -9, f"{B:.0f} cm", ha="center", va="top")
    ax.annotate("", (-bp / 2, Ht + 5), (bp / 2, Ht + 5), arrowprops={"arrowstyle": "<->", "lw": 0.7})
    ax.text(0, Ht + 7, f"{bp:.0f} cm", ha="center")
    ax.annotate("", (B / 2 + 6, hz), (B / 2 + 6, Ht), arrowprops={"arrowstyle": "<->", "lw": 0.7})
    ax.text(B / 2 + 8, (hz + Ht) / 2, f"{hp:.0f} cm", va="center")
    ax.annotate("", (B / 2 + 6, 0), (B / 2 + 6, hz), arrowprops={"arrowstyle": "<->", "lw": 0.7})
    ax.text(B / 2 + 8, hz / 2, f"{hz:.0f} cm", va="center")
    xl = B / 2 + 22
    for y, t, c in ((Ht - 6, f"{n_c} φ{dP * 10:.0f} por cara", "#1f4e9c"), (Ht - 14, f"E φ{eP * 10:.0f}@{sP:g} pedestal", "#d35400"),
                    (hz - 3, f"{nS} φ{dF * 10:.0f} sup. zapata", "#1f4e9c"), (3, f"{nI} φ{dF * 10:.0f} inf. zapata", "#1f4e9c"),
                    (hz / 2, f"E φ{eF * 10:.0f}@{sF:g} zapata", "#c0392b")):
        ax.text(xl, y, t, color=c, va="center")
    ax.set_xlim(-B / 2 - 15, B / 2 + 70)
    ax.set_ylim(-14, max(Ht, Df) + 14)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title(titulo or "Corte del sobrecimiento (cm)")
    return _png(fig)


def presiones_fundacion(fun: dict, titulo: str = "") -> bytes:
    """Presión de contacto bajo la fundación con la combinación de servicio más desfavorable."""
    s = fun.get("resumen_suelo") or {}
    est = [c for c in fun.get("servicio") or [] if c.get("estable")]
    c = next((x for x in est if x["combo"] == s.get("combo_presion")), max(est, key=lambda x: x["q_max"]) if est else None)
    fig, ax = plt.subplots(figsize=(8, 3.2))
    if c:
        xs, q = fun["x"], c["q"]
        ax.fill_between(xs, 0, q, color="#2d6cdf", alpha=0.25)
        ax.plot(xs, q, color="#2d6cdf", lw=1.2, label=f"q ({c['combo']})")
        if s.get("q_adm"):
            ax.axhline(s["q_adm"], color="#b00020", ls="--", lw=0.9, label=f"q adm = {s['q_adm']:g} tonf/m²")
        for ap in fun.get("apoyos") or []:
            x = ap.get("x") if isinstance(ap, dict) else ap[0]
            ax.axvline(x, color="#999", lw=0.5, ls=":")
        ax.invert_yaxis()
        ax.set_xlabel("x (m)")
        ax.set_ylabel("q (tonf/m²)")
        ax.legend(fontsize=7, loc="lower right")
        ax.grid(alpha=0.25, lw=0.4)
    ax.set_title(titulo or "Presión de contacto (servicio)")
    return _png(fig)


def planta_costaneras(datos: dict, res: dict, titulo: str = "") -> bytes:
    """Planta del techo: vigas de apoyo, costaneras (las verificadas coloreadas por FU), apoyos y tensores."""
    from app.sap.costaneras import ParametrosCostaneras, _geometria
    par = ParametrosCostaneras(**{k: v for k, v in (res.get("parametros") or {}).items()
                                  if k in ParametrosCostaneras.__dataclass_fields__ and k != "ids"})
    geo, _ = _geometria(datos, par)
    vig_ids = {a["viga"] for g in geo for a in g["apoyos"] if a.get("viga") is not None}
    vigas = [v for v in datos.get("vigas") or [] if v.get("id") in vig_ids]
    ver = {c["id"]: c for c in res.get("costaneras") or []}
    todos = [p for c in datos.get("costaneras") or [] for p in (c["p0"], c["p1"])]
    dx = (max(p[0] for p in todos) - min(p[0] for p in todos)) if todos else 1
    dy = (max(p[1] for p in todos) - min(p[1] for p in todos)) if todos else 1
    fig, ax = plt.subplots(figsize=(9, min(max(8 * dy / max(dx, 1e-6) + 1.2, 3.0), 9)))
    ax.add_collection(LineCollection([[v["p0"][:2], v["p1"][:2]] for v in vigas], colors="#9a9a9a", linewidths=1.2))
    xs, ys, lista = [], [], []
    for g in geo:
        c = next(x for x in datos["costaneras"] if x["id"] == g["id"])
        p0, p1 = c["p0"][:2], c["p1"][:2]
        xs += [p0[0], p1[0]]
        ys += [p0[1], p1[1]]
        r = ver.get(g["id"])
        ax.plot([p0[0], p1[0]], [p0[1], p1[1]], color=_color_razon(r["FU"]) if r and r.get("FU") is not None else "#b7c3d6",
                lw=2.4 if r else 0.9, solid_capstyle="round")
        L = g["L"]
        P = lambda x: (p0[0] + (p1[0] - p0[0]) * x / L, p0[1] + (p1[1] - p0[1]) * x / L)
        for a in g["apoyos"]:
            ax.plot(*P(a["x"]), "o", ms=2.5, color="#333")
        if r and r.get("FU") is not None:
            for tx in r.get("tensores_x") or []:
                ax.plot(*P(tx), "x", ms=3.5, color="#c06000", mew=1)
            tv = r.get("tramo_verificado") or {}
            lista.append((g["id"], r["FU"]))
            k = len(lista)
            # número en el tramo verificado (escalonado a lo largo para que no se tapen) y lista al pie
            f = 0.25 + 0.5 * ((k - 1) % 4) / 3
            xm = tv.get("x0", 0) + (tv.get("x1", L) - tv.get("x0", 0)) * f
            ax.annotate(str(k), P(xm), fontsize=6.5, fontweight="bold", color="white", ha="center", va="center",
                        bbox={"boxstyle": "circle,pad=0.2", "fc": _color_razon(r["FU"]), "ec": "none"})
    if xs:
        m = 0.04 * max(max(xs) - min(xs), max(ys) - min(ys), 1)
        ax.set_xlim(min(xs) - m, max(xs) + m)
    ax.set_aspect("equal", adjustable="datalim")
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    ax.grid(alpha=0.2, lw=0.4)
    ax.set_title(titulo or "Planta de costaneras: vigas de apoyo (gris), verificadas por FU, apoyos (●) y tensores (×)", fontsize=8)
    if lista:
        ax.text(0, -0.28, "   ".join(f"{k}: costanera {i} · FU {fu:.2f}" for k, (i, fu) in enumerate(lista, 1)), fontsize=7, va="top",
                transform=ax.transAxes)
    return _png(fig)


def momentos_costanera(res: dict, titulo: str = "") -> bytes:
    """Envolvente de momentos mayorados de la costanera más solicitada, con el tramo verificado sombreado."""
    con = [c for c in res.get("costaneras") or [] if c.get("FU") is not None and c.get("diagrama")]
    fig, ax = plt.subplots(figsize=(8, 3.2))
    if con:
        c = max(con, key=lambda x: x["FU"])
        dg = c["diagrama"]
        tv = c.get("tramo_verificado")
        if tv:
            ax.axvspan(tv["x0"], tv["x1"], color="#ffe08a", alpha=0.35, lw=0)
        ax.fill_between(dg["x"], 0, dg["Mmax"], color="#b00020", alpha=0.18)
        ax.plot(dg["x"], dg["Mmax"], color="#b00020", lw=1, label="Mx máx.")
        ax.fill_between(dg["x"], 0, dg["Mmin"], color="#2d6cdf", alpha=0.18)
        ax.plot(dg["x"], dg["Mmin"], color="#2d6cdf", lw=1, label="Mx mín.")
        for a in c["apoyos"]:
            ax.plot(a["x"], 0, "^", ms=6, color="#333")
        for tx in c.get("tensores_x") or []:
            ax.plot(tx, 0, "x", ms=5, color="#c06000", mew=1.2)
        ax.invert_yaxis()
        ax.axhline(0, color="#333", lw=0.6)
        ax.set_xlabel("x (m)")
        ax.set_ylabel("Mx (kgf·m)")
        ax.legend(fontsize=7)
        ax.grid(alpha=0.25, lw=0.4)
        ax.set_title(titulo or f"Costanera {c['id']} ({c.get('perfil')}): momentos mayorados; FU {c['FU']:.2f}")
    return _png(fig)
