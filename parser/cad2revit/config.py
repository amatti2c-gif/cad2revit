"""Carga y valida los archivos de entrada del proyectista (carpeta entradas/)."""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FUNCIONES_CAPA = {"estructura", "sustrato", "aislacion", "terminacion1", "terminacion2", "membrana"}

# Qué incluye cada preset de nivel de detalle (LOD).
PRESETS_LOD: dict[int, dict[str, Any]] = {
    100: dict(muros="generico", aberturas="ninguna", losas=True, cubierta=True, recintos=False,
              terminaciones=False, cielos=False, escaleras=False,
              columnas=False, vigas=False, fundaciones=False, ejes=True, entramado=False),
    200: dict(muros="tipo", aberturas="hueco", losas=True, cubierta=True, recintos=False,
              terminaciones=False, cielos=False, escaleras=False,
              columnas=True, vigas=False, fundaciones=False, ejes=True, entramado=False),
    300: dict(muros="compuesto", aberturas="familia", losas=True, cubierta=True, recintos=True,
              terminaciones=False, cielos=False, escaleras=False,
              columnas=True, vigas=True, fundaciones=True, ejes=True, entramado=False),
    350: dict(muros="compuesto", aberturas="familia", losas=True, cubierta=True, recintos=True,
              terminaciones=True, cielos=True, escaleras=True,
              columnas=True, vigas=True, fundaciones=True, ejes=True, entramado=True),
}

VALORES_ENUM = {
    "muros": {"generico", "tipo", "compuesto"},
    "aberturas": {"ninguna", "hueco", "familia"},
}


class ErrorConfiguracion(ValueError):
    """Error en los archivos de entrada; el mensaje está pensado para el usuario."""


@dataclass
class Configuracion:
    base: Path
    proyecto: dict
    materialidades: dict
    estructuracion: dict
    nivel_detalle: dict
    capas_cad: dict
    lod: dict = field(default_factory=dict)

    # --- accesos cómodos -----------------------------------------------------
    def capa(self, categoria: str) -> re.Pattern | None:
        patron = self.capas_cad.get("capas", {}).get(categoria)
        return re.compile(patron) if patron else None

    @property
    def deteccion(self) -> dict:
        d = {"espesor_min": 0.05, "espesor_max": 0.40, "largo_min": 0.20,
             "tolerancia_angulo_grados": 1.0, "vano_max": 3.0}
        d.update(self.capas_cad.get("deteccion_muros") or {})
        return d

    def tipo_muro(self, nombre: str) -> dict | None:
        return next((t for t in self.materialidades.get("tipos_muro", []) if t["nombre"] == nombre), None)

    def ruta(self, relativa: str) -> Path:
        p = Path(relativa)
        return p if p.is_absolute() else (self.base / p).resolve()


def _leer_yaml(ruta: Path) -> dict:
    if not ruta.exists():
        raise ErrorConfiguracion(f"No existe el archivo de entrada: {ruta}")
    with ruta.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolver_lod(nivel_detalle: dict) -> dict:
    preset = int(nivel_detalle.get("preset", 300))
    if preset not in PRESETS_LOD:
        raise ErrorConfiguracion(f"nivel_detalle.preset debe ser uno de {sorted(PRESETS_LOD)}, no {preset}")
    lod = copy.deepcopy(PRESETS_LOD[preset])
    for clave, valor in (nivel_detalle.get("ajustes") or {}).items():
        if clave not in lod:
            raise ErrorConfiguracion(f"nivel_detalle.ajustes: clave desconocida '{clave}'. Válidas: {sorted(lod)}")
        if clave in VALORES_ENUM and valor not in VALORES_ENUM[clave]:
            raise ErrorConfiguracion(f"nivel_detalle.ajustes.{clave} debe ser uno de {sorted(VALORES_ENUM[clave])}")
        lod[clave] = valor
    lod["preset"] = preset
    lod["altura_cielo"] = float(nivel_detalle.get("altura_cielo", 2.40))
    return lod


def _validar_materialidades(m: dict) -> list[str]:
    errores = []
    materiales = {x["nombre"] for x in m.get("materiales", [])}
    for grupo in ("tipos_muro", "tipos_losa", "tipos_cubierta"):
        for t in m.get(grupo, []):
            capas = t.get("capas") or []
            if not capas:
                errores.append(f"{grupo} '{t.get('nombre')}' no tiene capas")
            if not any(c.get("funcion") == "estructura" for c in capas):
                errores.append(f"{grupo} '{t.get('nombre')}' necesita al menos una capa con funcion: estructura")
            for c in capas:
                if c.get("funcion") not in FUNCIONES_CAPA:
                    errores.append(f"{grupo} '{t['nombre']}': función '{c.get('funcion')}' inválida "
                                   f"(usa {sorted(FUNCIONES_CAPA)})")
                if c.get("material") not in materiales:
                    errores.append(f"{grupo} '{t['nombre']}': el material '{c.get('material')}' "
                                   f"no está en la lista de materiales")
                if not (isinstance(c.get("espesor"), (int, float)) and c["espesor"] > 0):
                    errores.append(f"{grupo} '{t['nombre']}': espesor inválido en capa {c}")
            ent = t.get("entramado")
            if ent:
                if ent.get("material") not in materiales:
                    errores.append(f"{grupo} '{t['nombre']}': el material del entramado '{ent.get('material')}' "
                                   "no está en la lista de materiales")
                sec = ent.get("pie_derecho") or {}
                if not all(isinstance(sec.get(k), (int, float)) and sec[k] > 0 for k in ("b", "h")):
                    errores.append(f"{grupo} '{t['nombre']}': entramado.pie_derecho necesita b y h (m) mayores que 0")
                rv = ent.get("revit") or {}
                if rv.get("modo", "piezas") not in ("piezas", "familias", "solido"):
                    errores.append(f"{grupo} '{t['nombre']}': entramado.revit.modo debe ser 'piezas', 'solido' o 'familias'")
                if rv.get("modo") == "familias":
                    for k in ("familia_vertical", "familia_horizontal"):
                        if not (rv.get(k) or {}).get("familia"):
                            errores.append(f"{grupo} '{t['nombre']}': entramado.revit.{k} necesita 'familia' (y 'tipo')")
                for k in ("separacion_pies_derechos", "separacion_horizontales"):
                    if k in ent and not (isinstance(ent[k], (int, float)) and ent[k] >= 0.1):
                        errores.append(f"{grupo} '{t['nombre']}': entramado.{k} debe ser al menos 0.10 m")
            if t.get("capas"):
                t["espesor_total"] = round(sum(c.get("espesor", 0) for c in capas), 4)
    nombres_muro = {t["nombre"] for t in m.get("tipos_muro", [])}
    for r in m.get("reglas_muro", []):
        if r.get("tipo") not in nombres_muro:
            errores.append(f"reglas_muro: el tipo '{r.get('tipo')}' no está definido en tipos_muro")
        if "capa_cad" in r:
            try:
                re.compile(r["capa_cad"])
            except re.error as e:
                errores.append(f"reglas_muro: expresión regular inválida '{r['capa_cad']}': {e}")
    defecto = m.get("tipo_muro_por_defecto")
    if defecto and defecto not in nombres_muro:
        errores.append(f"tipo_muro_por_defecto '{defecto}' no está definido en tipos_muro")
    for clave, grupo in (("tipo_losa_por_defecto", "tipos_losa"), ("tipo_cubierta_por_defecto", "tipos_cubierta")):
        v = m.get(clave)
        if v and v not in {t["nombre"] for t in m.get(grupo, [])}:
            errores.append(f"{clave} '{v}' no está definido en {grupo}")
    return errores


def cargar(proyecto_yaml: str | Path) -> Configuracion:
    ruta = Path(proyecto_yaml).resolve()
    proyecto = _leer_yaml(ruta)
    base = ruta.parent

    def sub(clave: str, defecto: str) -> dict:
        return _leer_yaml(base / proyecto.get(clave, defecto))

    cfg = Configuracion(
        base=base,
        proyecto=proyecto,
        materialidades=sub("materialidades", "materialidades.yaml"),
        estructuracion=sub("estructuracion", "estructuracion.yaml"),
        nivel_detalle=sub("nivel_detalle", "nivel_detalle.yaml"),
        capas_cad=sub("capas_cad", "capas_cad.yaml"),
    )
    errores = _validar_materialidades(cfg.materialidades)
    mat_est = cfg.estructuracion.get("material_estructural")
    if mat_est and mat_est not in {x["nombre"] for x in cfg.materialidades.get("materiales", [])}:
        errores.append(f"estructuracion.material_estructural '{mat_est}' no está en materialidades.materiales")
    niveles = proyecto.get("niveles") or []
    if not niveles:
        errores.append("proyecto.niveles está vacío: define al menos un nivel")
    nombres_niv = {n["nombre"] for n in niveles}
    for p in proyecto.get("plantas") or []:
        if p.get("nivel") not in nombres_niv:
            errores.append(f"La planta '{p.get('archivo')}' apunta al nivel '{p.get('nivel')}', que no existe")
    if errores:
        raise ErrorConfiguracion("Errores en los archivos de entrada:\n  - " + "\n  - ".join(errores))
    cfg.lod = resolver_lod(cfg.nivel_detalle)
    return cfg
