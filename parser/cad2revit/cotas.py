"""Cotas de ejes: ajustar el modelo a una grilla de medidas cerradas (p. ej. la del calculista).

En proyecto.yaml:

    cotas_ejes:
      verticales:   {"1": 0, "2": 6.0, "5": 9.65, "6": 12.0, ...}     # posición de cada eje (m) desde el primero
      horizontales: {"A": 0, "B": 1.2, "C": 2.45, ...}

Cada eje de las cotas se ubica en su posición exacta (con un desplazamiento común que mueva lo menos posible el
modelo). Todo el plano se reajusta tramo a tramo entre ejes (interpolación lineal entre los ejes que calzan con
uno existente, a menos de 35 cm): muros, puertas, ventanas, rótulos y arcos quedan en la misma proporción dentro
de su vano entre ejes. Las ediciones hechas en la app (muros dibujados, quitados o editados, marcos, ejes) se
ajustan igual, para que sigan calzando.
"""
from __future__ import annotations

import copy
import math
import statistics
from dataclasses import replace

from . import ejes as ejes_mod

TOL = 0.35


class Reajuste:
    """Función de reajuste x -> x', y -> y' (lineal por tramos entre ejes; fuera de ellos, traslación)."""

    def __init__(self, pares_x: list[tuple[float, float]], pares_y: list[tuple[float, float]]):
        self.px = sorted(pares_x)
        self.py = sorted(pares_y)
        self.gx: list[tuple[float, float, float]] = []      # grupos (desde, hasta, desplazamiento) en X
        self.gy: list[tuple[float, float, float]] = []

    def agrupar(self, dib, ancho: float = 0.35) -> None:
        """Coordenadas muy cercanas (las dos caras de un muro, un marco de ventana) se mueven juntas: así el reajuste
        no cambia espesores. Grupos de valores separados menos de 25 cm, de a lo más `ancho` de extensión."""
        def grupos(valores, f):
            vs = sorted(set(round(v, 4) for v in valores))
            out, ini, ult = [], None, None
            for v in vs + [None]:
                if ini is not None and (v is None or v - ult > 0.25 or v - ini > ancho):
                    medio = (ini + ult) / 2
                    out.append((ini, ult, f(medio) - medio))
                    ini = None
                if v is not None:
                    if ini is None:
                        ini = v
                    ult = v
            return out
        xs, ys = [], []
        for sg in dib.segmentos:
            xs += [sg.a[0], sg.b[0]]; ys += [sg.a[1], sg.b[1]]
        for a in dib.arcos:
            for q in (a.centro, a.p0, a.p1):
                xs.append(q[0]); ys.append(q[1])
        self.gx = grupos(xs, lambda v: self._f(self.px, v))
        self.gy = grupos(ys, lambda v: self._f(self.py, v))

    @staticmethod
    def _g(grupos, pares, v):
        """Desplazamiento del grupo al que pertenece v (o el de la interpolación si no está en ninguno)."""
        lo, hi = 0, len(grupos)
        while lo < hi:                                   # búsqueda binaria por «desde»
            mid = (lo + hi) // 2
            if grupos[mid][0] <= v + 1e-6:
                lo = mid + 1
            else:
                hi = mid
        if lo and grupos[lo - 1][0] - 1e-6 <= v <= grupos[lo - 1][1] + 0.02:
            return v + grupos[lo - 1][2]
        return Reajuste._f(pares, v)

    @staticmethod
    def _f(pares, v):
        if not pares:
            return v
        if v <= pares[0][0]:
            return v + pares[0][1] - pares[0][0]
        if v >= pares[-1][0]:
            return v + pares[-1][1] - pares[-1][0]
        for (a0, b0), (a1, b1) in zip(pares, pares[1:]):
            if a0 <= v <= a1:
                return b0 + (b1 - b0) * (v - a0) / ((a1 - a0) or 1.0)
        return v

    def x(self, v):
        return self._g(self.gx, self.px, v) if self.gx else self._f(self.px, v)

    def y(self, v):
        return self._g(self.gy, self.py, v) if self.gy else self._f(self.py, v)

    def eje_x(self, v):                  # posiciones de ejes: interpolación exacta (sin grupos)
        return self._f(self.px, v)

    def eje_y(self, v):
        return self._f(self.py, v)

    def p_eje(self, q):
        return (round(self.eje_x(q[0]), 4), round(self.eje_y(q[1]), 4))

    def p(self, q):
        return (round(self.x(q[0]), 4), round(self.y(q[1]), 4))

    @staticmethod
    def _gi(grupos, pares, v):
        """Inversa exacta del reajuste con grupos: si v cae en el tramo reajustado de un grupo, se deshace su
        desplazamiento; si no, la interpolación inversa."""
        for g0, g1, d in grupos:
            if g0 + d - 1e-6 <= v <= g1 + d + 0.02:
                return v - d
        return Reajuste._f([(b, a) for a, b in pares], v)

    def p_inv(self, q):
        x = self._gi(self.gx, self.px, q[0]) if self.gx else self._f([(b, a) for a, b in self.px], q[0])
        y = self._gi(self.gy, self.py, q[1]) if self.gy else self._f([(b, a) for a, b in self.py], q[1])
        return (round(x, 4), round(y, 4))

    def inversa(self) -> "Reajuste":
        return Reajuste([(b, a) for a, b in self.px], [(b, a) for a, b in self.py])     # (sin grupos: aproximada)

    # ------------------------------------------------------------ aplicar
    def dibujo(self, dib):
        """Copia del dibujo con todas sus coordenadas reajustadas (no toca el dibujo original, que puede estar en caché)."""
        P = self.p
        return replace(
            dib,
            segmentos=[replace(s, a=P(s.a), b=P(s.b)) for s in dib.segmentos],
            polilineas_cerradas=[([P(q) for q in pts], capa) for pts, capa in dib.polilineas_cerradas],
            inserciones=[replace(i, punto=P(i.punto),
                                 extremos=(P(i.extremos[0]), P(i.extremos[1])) if i.extremos else None,
                                 esquinas=[P(q) for q in (i.esquinas or [])]) for i in dib.inserciones],
            textos=[replace(t, punto=P(t.punto)) for t in dib.textos],
            arcos=[replace(a, centro=P(a.centro), p0=P(a.p0), p1=P(a.p1)) for a in dib.arcos],
        )

    def ediciones(self, proyecto: dict) -> None:
        """Reajusta en memoria las coordenadas de las ediciones de la app (para que sigan calzando con el plano)."""
        for clave in ("muros_usuario", "muros_quitar", "muros_editar", "marcos_usuario"):
            for d in proyecto.get(clave) or []:
                for k in ("inicio", "fin"):
                    if d.get(k) and len(d[k]) == 2:
                        d[k] = list(self.p(d[k]))


def _es_vertical(e) -> bool:
    (x1, y1), (x2, y2) = e["inicio"], e["fin"]
    return abs(x2 - x1) < abs(y2 - y1)


def _pos(e) -> float:
    (x1, y1), (x2, y2) = e["inicio"], e["fin"]
    return (x1 + x2) / 2 if _es_vertical(e) else (y1 + y2) / 2


def preparar(proyecto: dict, adv: list[str]):
    """Calcula el reajuste a partir de las cotas y de los ejes actuales (ejes_base + ejes_usuario).
    Devuelve (reajuste, ajuste_ejes) o (None, None) si no hay cotas o no se puede."""
    cotas = proyecto.get("cotas_ejes") or {}
    if not cotas:
        return None, None
    base = proyecto.get("ejes_base")
    if not base:
        adv.append("Cotas de ejes: el proyecto aún no tiene los ejes fijados (ejecuta una vez y vuelve a intentar); se ignoran")
        return None, None
    actuales = ejes_mod.editar(copy.deepcopy(base), copy.deepcopy(proyecto.get("ejes_usuario") or {}), [])
    por = {e["nombre"]: e for e in actuales}
    pares = {"verticales": [], "horizontales": []}
    nuevas = {}                         # nombre -> (orientación, posición exacta)
    renombrar = {}                      # eje actual con el nombre de uno de las cotas, pero que no calza con él
    for grupo, vertical in (("verticales", True), ("horizontales", False)):
        c = {str(k): float(v) for k, v in (cotas.get(grupo) or {}).items()}
        if not c:
            continue
        mismos = [n for n in c if n in por and _es_vertical(por[n]) == vertical]
        if not mismos:
            adv.append(f"Cotas de ejes ({grupo}): ningún eje del modelo tiene esos nombres; se ignoran")
            continue
        off = statistics.median(_pos(por[n]) - c[n] for n in mismos)
        calzan = [n for n in mismos if abs(_pos(por[n]) - (off + c[n])) <= TOL]
        if calzan:
            off = sum(_pos(por[n]) - c[n] for n in calzan) / len(calzan)
            calzan = [n for n in mismos if abs(_pos(por[n]) - (off + c[n])) <= TOL]
        for n in calzan:
            pares[grupo].append((_pos(por[n]), off + c[n]))
        for n, v in c.items():
            nuevas[n] = (vertical, off + v)
            if n in por and n not in calzan:
                renombrar[n] = n + "'"
        movs = [abs(_pos(por[n]) - (off + c[n])) for n in calzan]
        adv.append(f"Cotas de ejes ({grupo}): {len(calzan)} ejes ajustados a sus cotas (movimiento máximo "
                   f"{max(movs) * 100 if movs else 0:.0f} cm)"
                   + (f"; {len(c) - len(calzan)} nuevos: {', '.join(n for n in c if n not in calzan)}" if len(c) > len(calzan) else ""))
    if renombrar:
        adv.append("Cotas de ejes: ejes del modelo con el mismo nombre que uno de las cotas pero en otra posición, "
                   "renombrados: " + ", ".join(f"{a} → {b}" for a, b in renombrar.items()))
    reajuste = Reajuste(pares["verticales"], pares["horizontales"])
    return reajuste, {"nuevas": nuevas, "renombrar": renombrar, "actuales": por}


def ejes_usuario_reajustados(proyecto: dict, reajuste: Reajuste, actuales: dict) -> dict:
    """ejes_usuario con sus coordenadas reajustadas (posiciones y trazados)."""
    out = {}
    for n, c in (proyecto.get("ejes_usuario") or {}).items():
        if c is None:
            out[n] = None
            continue
        c = dict(c)
        if c.get("inicio") and c.get("fin"):
            c["inicio"], c["fin"] = list(reajuste.p_eje(c["inicio"])), list(reajuste.p_eje(c["fin"]))
        if c.get("posicion") is not None and n in actuales:
            c["posicion"] = reajuste.eje_x(c["posicion"]) if _es_vertical(actuales[n]) else reajuste.eje_y(c["posicion"])
        out[n] = c
    return out


def aplicar_a_ejes(ejes: list[dict], ajuste: dict, adv: list[str]) -> list[dict]:
    """Después de reajustar: renombra los ejes en conflicto, deja cada eje de las cotas en su posición exacta y crea
    los que faltan (a lo largo de todos los ejes perpendiculares)."""
    por = {}
    for e in ejes:
        e = dict(e)
        if e["nombre"] in ajuste["renombrar"]:
            e["nombre"] = ajuste["renombrar"][e["nombre"]]
        por[e["nombre"]] = e
    for n, (vertical, v) in ajuste["nuevas"].items():
        e = por.get(n)
        if e is not None and _es_vertical(e) == vertical:
            (x1, y1), (x2, y2) = e["inicio"], e["fin"]
            if vertical:
                e["inicio"], e["fin"] = [round(v, 4), y1], [round(v, 4), y2]
            else:
                e["inicio"], e["fin"] = [x1, round(v, 4)], [x2, round(v, 4)]
            continue
        perp = [x for x in por.values() if _es_vertical(x) != vertical]
        if not perp:
            continue
        lo = min(_pos(x) for x in perp) - 0.6
        hi = max(_pos(x) for x in perp) + 0.6
        por[n] = {"nombre": n, "inicio": [round(v, 4), lo] if vertical else [lo, round(v, 4)],
                  "fin": [round(v, 4), hi] if vertical else [hi, round(v, 4)], "origen": "cotas"}
        adv.append(f"Cotas de ejes: eje {n} creado según las cotas")
    return list(por.values())


def para_proyecto(cfg):
    """Reajuste del proyecto con sus grupos (los de la primera planta), para convertir coordenadas entre el plano
    original y el modelo reajustado (la app muestra el reajustado; las ediciones se guardan en el original)."""
    from .dxf_reader import leer
    from . import vistas as vistas_mod
    reaj, ajuste = preparar(cfg.proyecto, [])
    if not reaj:
        return None
    plantas = cfg.proyecto.get("plantas") or []
    if plantas:
        try:
            dib = vistas_mod.recortar(leer(cfg.ruta(plantas[0]["archivo"]), cfg.capas_cad.get("unidades", "auto")),
                                      vistas_mod.zonas_de(plantas[0]))
            reaj.agrupar(dib)
        except Exception:
            pass
    return reaj


def convertir_ediciones(ed: dict, f) -> dict:
    """Aplica la función de punto f a las coordenadas de las ediciones (formato de /ediciones)."""
    out = dict(ed)
    for k in ("muros", "quitar", "marcos", "editar"):
        lista = []
        for d in ed.get(k) or []:
            d = dict(d)
            for c in ("inicio", "fin"):
                if isinstance(d.get(c), list) and len(d[c]) == 2:
                    d[c] = list(f(d[c]))
            lista.append(d)
        out[k] = lista
    return out
