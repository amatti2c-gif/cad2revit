"""Marcos de acero: de momento y arriostrados en cruz.

En planta, cada marco se dibuja como una línea (su eje) en una capa de marcos.
Por cada línea y cada piso se generan:
  - columnas en los extremos (y en divisiones intermedias si el marco supera
    el vano máximo del lego),
  - una viga por vano en el nivel superior,
  - en marcos arriostrados, dos diagonales en X por vano.
Las columnas compartidas por dos marcos que se tocan se crean una sola vez.
"""
from __future__ import annotations

import math
import re

from .dxf_reader import Segmento

SISTEMAS = {"momento", "arriostrado_x"}
FORMAS = {"W", "C", "L", "tubo", "rectangular", "circular"}
TECHOS = {"horizontal", "dos_aguas"}
PENDIENTE_POR_DEFECTO = 15.0          # grados, si el lego es a dos aguas y el edificio no tiene techo inclinado


class Planos(list):
    """Lista de planos del techo que además recuerda el contorno de la cubierta."""
    contorno: list | None = None


def _dentro(pts, p) -> bool:
    x, y, c = p[0], p[1], False
    for i in range(len(pts)):
        (x1, y1), (x2, y2) = pts[i], pts[i - 1]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            c = not c
    return c


def _mas_cercano_en_borde(pts, p):
    mejor, dm = p, float("inf")
    for i in range(len(pts)):
        a, b = pts[i - 1], pts[i]
        dx, dy = b[0] - a[0], b[1] - a[1]
        L2 = dx * dx + dy * dy or 1e-12
        k = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2))
        q = (a[0] + dx * k, a[1] + dy * k)
        d = math.dist(p, q)
        if d < dm:
            mejor, dm = q, d
    return mejor


def planos_de_cubierta(cub: dict | None) -> list[tuple]:
    """Planos del techo inclinado del edificio, uno por borde con pendiente: (c0, cx, cy, a, b) con
    h(x, y) = c0 + cx*x + cy*y la altura sobre el alero, y a-b el borde que lo origina."""
    if not cub or not cub.get("pendiente") or not cub.get("bordes_pendiente"):
        return []
    pts = [tuple(p) for p in cub["contorno"]]
    n = len(pts)
    bordes = list(cub["bordes_pendiente"])
    area = sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1] for i in range(n))
    if area < 0:                                  # antihorario: el interior queda a la izquierda
        pts = pts[::-1]
        bordes = [n - 1 if i == n - 1 else n - 2 - i for i in bordes]
    t = math.tan(math.radians(cub["pendiente"]))
    planos = Planos()
    planos.contorno = pts
    for i in bordes:
        a, b = pts[i], pts[(i + 1) % n]
        L = math.dist(a, b) or 1.0
        nx, ny = -(b[1] - a[1]) / L, (b[0] - a[0]) / L
        planos.append((-(a[0] * nx + a[1] * ny) * t, nx * t, ny * t, a, b))
    return planos


def altura_techo(planos: list, p) -> float:
    """Altura del techo sobre el alero en el punto p. Cada plano actúa solo de su lado del borde y frente a él
    (con un margen de 45°): así un acceso a dos aguas perpendicular no corta el resto del techo."""
    contorno = getattr(planos, "contorno", None)
    if contorno and not _dentro(contorno, p):
        # fuera de la cubierta (p. ej. un eje que sale del edificio): la altura del borde más cercano,
        # o sea el alero (0) o el perfil del frontón
        p = _mas_cercano_en_borde(contorno, p)
    validos, todos = [], []
    for c0, cx, cy, a, b in planos:
        hv = c0 + cx * p[0] + cy * p[1]
        L = math.dist(a, b) or 1.0
        ux, uy = (b[0] - a[0]) / L, (b[1] - a[1]) / L
        tp = (p[0] - a[0]) * ux + (p[1] - a[1]) * uy
        if hv < -1e-9:
            if -1e-6 <= tp <= L + 1e-6:
                return 0.0                        # más allá del alero (p. ej. un eje que sale del edificio)
            continue
        todos.append(hv)
        d = abs((p[0] - a[0]) * uy - (p[1] - a[1]) * ux)
        if -d - 1e-6 <= tp <= L + d + 1e-6:
            validos.append(hv)
    lista = validos or todos
    return max(0.0, min(lista)) if lista else 0.0


def _perfil_techo(a, b, planos, pendiente: float, cortes: list[float]) -> tuple[list[float], callable]:
    """Puntos de quiebre de la viga a lo largo del marco (t en [0, L]) y la altura h(t) sobre el nivel.
    Con planos del edificio la viga sigue el techo; si no, dos aguas simétrico con la pendiente dada."""
    L = math.dist(a, b)
    u = ((b[0] - a[0]) / L, (b[1] - a[1]) / L)
    if planos:
        def h(t):
            return altura_techo(planos, (a[0] + u[0] * t, a[1] + u[1] * t))
        # quiebres (cumbreras, limahoyas): cambios de pendiente a lo largo de la línea, muestreando cada 5 cm
        n = max(20, int(L / 0.05))
        ts_m = [L * i / n for i in range(n + 1)]
        hs = [h(t) for t in ts_m]
        pend = [(hs[i + 1] - hs[i]) / (ts_m[i + 1] - ts_m[i]) for i in range(n)]
        quiebres = sorted(ts_m[i] for i in range(1, n) if abs(pend[i] - pend[i - 1]) > 0.05)
        # de cada grupo de quiebres seguidos se deja el punto exacto: intersección de las dos rectas vecinas
        limpios, grupo = [], []
        for t in quiebres + [None]:
            if grupo and (t is None or t - grupo[-1] > 0.11):
                i0 = max(0, ts_m.index(grupo[0]) - 1)
                i1 = min(n, ts_m.index(grupo[-1]) + 1)
                m0 = pend[i0] if i0 < n else 0.0
                m1 = pend[i1] if i1 < n else 0.0
                if abs(m0 - m1) > 1e-9:          # cruce de h(t0)+m0(t-t0) con h(t1)+m1(t-t1)
                    t0, t1 = ts_m[i0], ts_m[i1]
                    tx = (hs[i1] - hs[i0] + m0 * t0 - m1 * t1) / (m0 - m1)
                    limpios.append(round(min(max(tx, t0), t1), 6))
                else:
                    limpios.append(max(grupo, key=h))
                grupo = []
            if t is not None:
                grupo.append(t)
        return sorted(set(cortes) | {0.0, L} | set(limpios)), h
    tg = math.tan(math.radians(pendiente))
    lin = [(0.0, tg), (L * tg, -tg)]

    def h(t):
        return max(0.0, min(p + q * t for p, q in lin))
    ts = set(cortes) | {0.0, L}
    if 1e-3 < L / 2 < L - 1e-3:
        ts.add(round(L / 2, 6))
    return sorted(ts), h


def tipo_para_capa(capa: str, cfg) -> str | None:
    """Lego de marco para una capa del CAD: el elegido en el proyecto o, si no, las reglas."""
    elegido = ((cfg.proyecto.get("legos") or {}).get("marcos") or {}).get(capa)
    if elegido:
        return elegido
    for r in cfg.materialidades.get("reglas_marco", []):
        if re.search(r.get("capa_cad", ""), capa):
            return r.get("tipo")
    return cfg.materialidades.get("tipo_marco_por_defecto")


def _perfil(p: dict, material: str, rol: str) -> dict:
    return {"rol": rol, "material": material, "forma": p.get("forma", "W"),
            "perfil": p.get("perfil") or None, "familia": p.get("familia") or None,
            "b": float(p.get("b", 0.15)), "h": float(p.get("h", 0.2))}


def generar(segmentos: list, cfg, nivel: str, sup: str | None, cota: dict[str, float],
            prefijo: str, advertencias: list[str], columnas_vistas: set | None = None,
            numero_inicial: int = 1, planos_techo: list | None = None) -> list[dict]:
    """segmentos: Segmento del CAD, o tuplas (Segmento, lego, origen) para marcos que no vienen
    de una capa (p. ej. los propuestos por tramo de eje)."""
    items = [x if isinstance(x, tuple) else (x, None, None) for x in segmentos]
    if sup is None:
        if items:
            advertencias.append(f"{nivel}: hay marcos pero no existe un nivel superior; se omiten")
        return []
    z0, z1 = cota[nivel], cota[sup]
    out = []
    if columnas_vistas is None:
        columnas_vistas = set()
    for k, (s, lego_fijo, origen) in enumerate(sorted(items, key=lambda x: (x[0].a, x[0].b)), numero_inicial):
        nombre = lego_fijo or tipo_para_capa(s.capa, cfg)
        lego = next((t for t in cfg.materialidades.get("tipos_marco", []) if t["nombre"] == nombre), None)
        if lego is None:
            advertencias.append(f"Marco en capa '{s.capa}': no hay lego de marco asignado; se omite")
            continue
        largo = s.largo
        if largo < 0.3:
            continue
        vano_max = float(lego.get("vano_maximo") or 0)
        n = max(1, math.ceil(largo / vano_max - 1e-9)) if vano_max > 0 else 1
        pts = [(round(s.a[0] + (s.b[0] - s.a[0]) * i / n, 4), round(s.a[1] + (s.b[1] - s.a[1]) * i / n, 4))
               for i in range(n + 1)]
        mat = lego["material"]
        techo = lego.get("techo", "horizontal")
        u = ((s.b[0] - s.a[0]) / largo, (s.b[1] - s.a[1]) / largo)
        en = lambda t: (round(s.a[0] + u[0] * t, 4), round(s.a[1] + u[1] * t, 4))   # noqa: E731
        cortes = [largo * i / n for i in range(n + 1)]
        if techo == "dos_aguas":
            # bajo el techo inclinado del edificio la viga lo sigue; si no, dos aguas propio del marco
            ts, h = _perfil_techo(s.a, s.b, planos_techo or [], float(lego.get("pendiente") or PENDIENTE_POR_DEFECTO), cortes)
            cumbre = max(ts, key=h)
            if 0.3 < cumbre < largo - 0.3 and h(cumbre) > 1e-3:
                if lego.get("pilar_central"):
                    # pilar bajo la cumbrera; el vano máximo subdivide cada mitad si hace falta
                    mitades = [(0.0, cumbre), (cumbre, largo)]
                    cortes = sorted({round(t0 + (t1 - t0) * i / max(1, math.ceil((t1 - t0) / vano_max - 1e-9)), 6)
                                     if vano_max > 0 else t0
                                     for t0, t1 in mitades
                                     for i in range((max(1, math.ceil((t1 - t0) / vano_max - 1e-9)) if vano_max > 0 else 1) + 1)}
                                    | {0.0, cumbre, largo})
                else:
                    cortes = [0.0, largo]                     # vano libre: columnas solo en los extremos
                ts, h = _perfil_techo(s.a, s.b, planos_techo or [],
                                      float(lego.get("pendiente") or PENDIENTE_POR_DEFECTO), cortes)
                n = len(cortes) - 1
        else:
            ts, h = sorted(cortes), (lambda t: 0.0)
        alto = lambda t: round(z1 + h(t), 4)          # noqa: E731
        cols = []
        for t in cortes:
            p = en(t)
            clave = (round(p[0], 3), round(p[1], 3))
            if clave in columnas_vistas:
                continue                      # columna ya creada por otro marco del mismo piso
            columnas_vistas.add(clave)
            cols.append({"punto": list(p), "z0": z0, "z1": alto(t)})
        vigas = [{"inicio": [*en(t0), alto(t0)], "fin": [*en(t1), alto(t1)]} for t0, t1 in zip(ts, ts[1:])]
        diagonales = []
        if lego["sistema"] == "arriostrado_x":
            for t0, t1 in zip(cortes, cortes[1:]):
                a, b = en(t0), en(t1)
                diagonales.append({"inicio": [a[0], a[1], z0], "fin": [b[0], b[1], alto(t1)]})
                diagonales.append({"inicio": [b[0], b[1], z0], "fin": [a[0], a[1], alto(t0)]})
        out.append({
            "id": f"{prefijo}-F{k:03d}", "nivel": nivel, "nivel_superior": sup, "tipo": nombre,
            "sistema": lego["sistema"], "material": mat, "capa_cad": s.capa, "origen": origen or "plano CAD",
            "inicio": list(s.a), "fin": list(s.b), "vanos": n,
            "techo": techo if techo == "horizontal" else ("sigue la cubierta" if planos_techo else
                                                         f"dos aguas {float(lego.get('pendiente') or PENDIENTE_POR_DEFECTO):g}°"),
            "altura_maxima": round(max(alto(t) for t in ts) - z0, 3),
            "direccion": [round((s.b[0] - s.a[0]) / largo, 6), round((s.b[1] - s.a[1]) / largo, 6)],
            "perfiles": {
                "columna": _perfil(lego["columna"], mat, "columna"),
                "viga": _perfil(lego["viga"], mat, "viga"),
                **({"diagonal": _perfil(lego["diagonal"], mat, "diagonal")} if lego["sistema"] == "arriostrado_x" else {}),
            },
            "columnas": cols, "vigas": vigas, "diagonales": diagonales,
        })
    return out


def validar(m: dict) -> list[str]:
    """Reglas de los legos de marco (tipos_marco de materialidades.yaml)."""
    errores = []
    materiales = {x["nombre"] for x in m.get("materiales", [])}
    for t in m.get("tipos_marco", []):
        n = t.get("nombre", "?")
        if t.get("sistema") not in SISTEMAS:
            errores.append(f"marco '{n}': sistema debe ser 'momento' o 'arriostrado_x'")
        if t.get("material") not in materiales:
            errores.append(f"marco '{n}': el material '{t.get('material')}' no está en la lista de materiales")
        roles = ["columna", "viga"] + (["diagonal"] if t.get("sistema") == "arriostrado_x" else [])
        for rol in roles:
            p = t.get(rol)
            if not isinstance(p, dict):
                errores.append(f"marco '{n}': falta el perfil de {rol}")
                continue
            if p.get("forma") not in FORMAS:
                errores.append(f"marco '{n}': forma de {rol} '{p.get('forma')}' inválida (usa {sorted(FORMAS)})")
            for k in ("b", "h"):
                if not (isinstance(p.get(k), (int, float)) and p[k] > 0):
                    errores.append(f"marco '{n}': {rol}.{k} debe ser un número mayor que 0 (m)")
        if t.get("pilar_central") is not None and not isinstance(t["pilar_central"], bool):
            errores.append(f"marco '{n}': pilar_central debe ser true o false")
        if t.get("techo", "horizontal") not in TECHOS:
            errores.append(f"marco '{n}': techo debe ser 'horizontal' o 'dos_aguas'")
        if t.get("pendiente") is not None and not (isinstance(t["pendiente"], (int, float)) and 0 < t["pendiente"] <= 60):
            errores.append(f"marco '{n}': pendiente debe estar entre 0 y 60 grados")
        if t.get("vano_maximo") is not None and not (isinstance(t["vano_maximo"], (int, float)) and t["vano_maximo"] >= 1):
            errores.append(f"marco '{n}': vano_maximo debe ser al menos 1 m (o vacío)")
    nombres = {t.get("nombre") for t in m.get("tipos_marco", [])}
    for r in m.get("reglas_marco", []):
        if r.get("tipo") not in nombres:
            errores.append(f"reglas_marco: el tipo '{r.get('tipo')}' no está definido en tipos_marco")
    d = m.get("tipo_marco_por_defecto")
    if d and d not in nombres:
        errores.append(f"tipo_marco_por_defecto '{d}' no está definido en tipos_marco")
    return errores
