"""Aplicación web de cad2revit.

Permite, desde el navegador: crear proyectos, subir planos DXF, editar los
lineamientos (YAML), ejecutar el parser, revisar el resultado en 2D/3D y llevar
un historial de corridas para iterar (notas, comparar, restaurar).

    uvicorn app.servidor:app --reload        # desde la raíz del repositorio

Variables de entorno:
    CAD2REVIT_DATOS   carpeta donde se guardan proyectos (por defecto ./datos)
    APP_USERNAME / APP_PASSWORD
                      usuario y clave de acceso (HTTP Basic). En Railway son
                      obligatorias: si faltan, la app queda bloqueada (503) en
                      vez de quedar abierta. En local, sin ellas, queda abierta.
"""
from __future__ import annotations

import base64
import binascii
import difflib
import json
import os
import re
import secrets
import shutil
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "parser"))
sys.path.insert(0, str(RAIZ / "ejemplos"))

from cad2revit import __version__  # noqa: E402
from cad2revit.config import ErrorConfiguracion, cargar  # noqa: E402
from cad2revit.dxf_reader import leer  # noqa: E402
from cad2revit.modelo import construir_modelo  # noqa: E402
from cad2revit.validar import validar  # noqa: E402

# Lineamientos COMUNES (uno solo en la app, para todos los proyectos) + proyecto.yaml (propio)
LINEAMIENTOS = ["materialidades.yaml", "estructuracion.yaml", "nivel_detalle.yaml", "capas_cad.yaml"]
ARCHIVOS_ENTRADA = ["proyecto.yaml", "materialidades.yaml", "estructuracion.yaml",
                    "nivel_detalle.yaml", "capas_cad.yaml"]
NOMBRE_SEGURO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\- ]{0,120}$")
MAX_SUBIDA = 50 * 1024 * 1024
MAX_SEGMENTOS_VISTA = 40000


def _datos() -> Path:
    d = Path(os.environ.get("CAD2REVIT_DATOS", RAIZ / "datos")).resolve()
    (d / "proyectos").mkdir(parents=True, exist_ok=True)
    return d


app = FastAPI(title="cad2revit", version=__version__)


# ---------------------------------------------------------------- acceso
def _credenciales() -> tuple[str, str]:
    return os.environ.get("APP_USERNAME", ""), os.environ.get("APP_PASSWORD", "")


def _en_servidor() -> bool:
    """True cuando corre publicada (Railway define estas variables)."""
    return any(os.environ.get(v) for v in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
                                           "RAILWAY_PROJECT_ID", "CAD2REVIT_EXIGIR_CLAVE"))


@app.middleware("http")
async def _autenticacion_basica(request: Request, call_next):
    if request.url.path == "/salud":           # chequeo de salud de Railway
        return await call_next(request)
    usuario_ok, clave_ok = _credenciales()
    if not usuario_ok or not clave_ok:
        if _en_servidor():
            return Response(status_code=503,
                            content="Acceso bloqueado: faltan configurar APP_USERNAME y APP_PASSWORD.")
        return await call_next(request)         # uso local sin clave
    enc = request.headers.get("authorization", "")
    if enc.lower().startswith("basic "):
        try:
            usuario, _, clave = base64.b64decode(enc[6:]).decode("utf-8").partition(":")
            # se comparan bytes UTF-8: compare_digest no acepta str con tildes o ñ
            if (secrets.compare_digest(usuario.encode("utf-8"), usuario_ok.encode("utf-8"))
                    and secrets.compare_digest(clave.encode("utf-8"), clave_ok.encode("utf-8"))):
                return await call_next(request)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            pass
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="cad2revit"'})


@app.get("/salud")
def salud():
    # Railway informa el commit desplegado; sirve para saber qué versión está en línea
    commit = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_COMMIT") or "")[:7]
    return {"ok": True, "version": __version__, "commit": commit or "local",
            "tipos_madera": "entramado" in (RAIZ / "entradas" / "materialidades.yaml").read_text(encoding="utf-8")}


# ---------------------------------------------------------------- utilidades
def _seguro(nombre: str, que: str = "nombre") -> str:
    if not NOMBRE_SEGURO.match(nombre) or ".." in nombre:
        raise HTTPException(400, f"{que} inválido: '{nombre}'")
    return nombre


def _slug(texto: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", texto.lower().translate(str.maketrans("áéíóúñü", "aeiounu"))).strip("-")
    return s[:60] or "proyecto"


def _lineamientos() -> Path:
    """Carpeta única de lineamientos de la app. La primera vez se crea con los valores
    por defecto; desde ahí lo que se edite en la app es lo vigente para todos los proyectos."""
    d = _datos() / "lineamientos"
    if not d.exists():
        d.mkdir(parents=True)
        for a in LINEAMIENTOS:
            shutil.copy2(RAIZ / "entradas" / a, d / a)
    return d


def _ruta_entrada(d: Path, archivo: str) -> Path:
    return d / "entradas" / archivo if archivo == "proyecto.yaml" else _lineamientos() / archivo


def _enlazar_lineamientos(d: Path) -> None:
    """Hace que proyecto.yaml use los lineamientos comunes de la app (y migra proyectos
    antiguos que tenían copias propias)."""
    py = d / "entradas" / "proyecto.yaml"
    if not py.exists():
        return
    texto = original = py.read_text(encoding="utf-8")
    rel = os.path.relpath(_lineamientos(), d / "entradas").replace(os.sep, "/")
    for a in LINEAMIENTOS:
        clave = a.removesuffix(".yaml")
        linea = f"{clave}: {rel}/{a}"
        if re.search(rf"^{clave}:.*$", texto, flags=re.M):
            texto = re.sub(rf"^{clave}:.*$", linea, texto, count=1, flags=re.M)
        else:
            texto = texto.rstrip() + "\n" + linea + "\n"
    if texto != original:
        py.write_text(texto, encoding="utf-8")
    for a in LINEAMIENTOS:                       # las copias antiguas ya no se usan
        (d / "entradas" / a).unlink(missing_ok=True)


def _texto_proyecto_inicial(nombre: str) -> str:
    texto = (RAIZ / "entradas" / "proyecto.yaml").read_text(encoding="utf-8").replace("../ejemplos/", "../planos/")
    return re.sub(r"^nombre:.*$", f"nombre: {json.dumps(nombre, ensure_ascii=False)}", texto, count=1, flags=re.M)


def _reparar_proyecto(d: Path) -> None:
    """Si proyecto.yaml perdió sus niveles (archivo dañado), se reconstruye desde la plantilla
    conservando la selección de legos. Las plantas apuntan a los planos del proyecto."""
    py = d / "entradas" / "proyecto.yaml"
    datos = _leer_yaml_seguro(py) if py.exists() else {}
    if datos.get("niveles"):
        return
    texto = _texto_proyecto_inicial(_meta(d).get("nombre", d.name))
    if datos.get("legos"):
        texto += "\n# Legos elegidos para este proyecto (se editan en la pestaña Legos de la app)\n"
        texto += yaml.safe_dump({"legos": datos["legos"]}, allow_unicode=True, sort_keys=False)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text(texto, encoding="utf-8")
    (d / "reparado.txt").write_text(f"{_ahora()}: proyecto.yaml sin niveles; reconstruido desde la plantilla\n",
                                    encoding="utf-8")


def _proyecto(pid: str) -> Path:
    d = _datos() / "proyectos" / _seguro(pid, "proyecto")
    if not (d / "meta.json").exists():
        raise HTTPException(404, f"No existe el proyecto '{pid}'")
    _reparar_proyecto(d)
    _enlazar_lineamientos(d)
    return d


def _meta(d: Path) -> dict:
    return json.loads((d / "meta.json").read_text(encoding="utf-8"))


def _corrida_dir(d: Path, n: int) -> Path:
    c = d / "corridas" / f"{n:04d}"
    if not c.exists():
        raise HTTPException(404, f"No existe la corrida {n}")
    return c


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validar_entradas(d: Path) -> list[str]:
    try:
        cargar(d / "entradas" / "proyecto.yaml")
        return []
    except ErrorConfiguracion as e:
        return [l.strip(" -") for l in str(e).splitlines()[1:]] or [str(e)]
    except Exception as e:  # YAML mal formado en otro archivo, etc.
        return [f"{type(e).__name__}: {e}"]


# ---------------------------------------------------------------- proyectos
class NuevoProyecto(BaseModel):
    nombre: str
    con_ejemplo: bool = True


@app.get("/api/proyectos")
def listar_proyectos():
    out = []
    for d in sorted((_datos() / "proyectos").iterdir()):
        if (d / "meta.json").exists():
            m = _meta(d)
            m["corridas"] = len(list((d / "corridas").glob("[0-9]*")))
            out.append(m)
    return out


@app.post("/api/proyectos", status_code=201)
def crear_proyecto(datos: NuevoProyecto):
    nombre = datos.nombre.strip()
    if not nombre:
        raise HTTPException(400, "El nombre no puede estar vacío")
    base = _slug(nombre)
    pid, k = base, 2
    while (_datos() / "proyectos" / pid).exists():
        pid, k = f"{base}-{k}", k + 1
    d = _datos() / "proyectos" / pid
    (d / "planos").mkdir(parents=True)
    (d / "corridas").mkdir()
    (d / "entradas").mkdir()
    # Solo proyecto.yaml es propio del proyecto; los lineamientos son los comunes de la app
    py = d / "entradas" / "proyecto.yaml"
    py.write_text(_texto_proyecto_inicial(nombre), encoding="utf-8")
    if datos.con_ejemplo:
        from generar_ejemplo import generar
        generar(d / "planos")
    _enlazar_lineamientos(d)
    meta = {"id": pid, "nombre": nombre, "creado": _ahora(), "con_ejemplo": datos.con_ejemplo}
    (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta


@app.delete("/api/proyectos/{pid}", status_code=204)
def borrar_proyecto(pid: str):
    shutil.rmtree(_proyecto(pid))


# ---------------------------------------------------------------- lineamientos
class TextoEntrada(BaseModel):
    texto: str


@app.get("/api/proyectos/{pid}/entradas")
def leer_entradas(pid: str):
    d = _proyecto(pid)
    return {
        "archivos": {a: _ruta_entrada(d, a).read_text(encoding="utf-8") for a in ARCHIVOS_ENTRADA},
        "errores": _validar_entradas(d),
    }


@app.put("/api/proyectos/{pid}/entradas/{archivo}")
def guardar_entrada(pid: str, archivo: str, datos: TextoEntrada):
    d = _proyecto(pid)
    if archivo not in ARCHIVOS_ENTRADA:
        raise HTTPException(404, f"Archivo de entrada desconocido: {archivo}")
    try:
        contenido = yaml.safe_load(datos.texto)
    except yaml.YAMLError as e:
        marca = getattr(e, "problem_mark", None)
        donde = f" (línea {marca.line + 1}, columna {marca.column + 1})" if marca else ""
        raise HTTPException(400, f"YAML mal formado{donde}: {getattr(e, 'problem', e)}")
    if archivo == "proyecto.yaml" and not (isinstance(contenido, dict) and contenido.get("niveles")):
        raise HTTPException(400, "proyecto.yaml debe tener la lista 'niveles' (con al menos un nivel); no se guardó")
    if archivo != "proyecto.yaml" and not isinstance(contenido, dict):
        raise HTTPException(400, f"{archivo} quedaría vacío; no se guardó")
    _ruta_entrada(d, archivo).write_text(datos.texto, encoding="utf-8")
    if archivo == "proyecto.yaml":
        _enlazar_lineamientos(d)
    return {"guardado": True, "errores": _validar_entradas(d)}


# ---------------------------------------------------------------- legos (catálogo y selección)
def _leer_yaml_seguro(ruta: Path) -> dict:
    try:
        return yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


@app.get("/api/catalogo")
def catalogo():
    """Piezas prearmadas disponibles (salen de los lineamientos comunes de la app)."""
    lin = _lineamientos()
    mat = _leer_yaml_seguro(lin / "materialidades.yaml")
    est = _leer_yaml_seguro(lin / "estructuracion.yaml")
    colores = {m["nombre"]: m.get("color") for m in mat.get("materiales", [])}
    clases = {m["nombre"]: m.get("clase") for m in mat.get("materiales", [])}

    def tarjeta(t: dict) -> dict:
        capas = [dict(c, color=colores.get(c.get("material"))) for c in t.get("capas") or []]
        return {"nombre": t["nombre"], "estructural": bool(t.get("estructural")),
                "espesor_total": round(sum(c.get("espesor", 0) for c in capas), 4),
                "capas": capas, "entramado": t.get("entramado"),
                "clase_entramado": clases.get((t.get("entramado") or {}).get("material"))}

    return {
        "muros": [tarjeta(t) for t in mat.get("tipos_muro", [])],
        "losas": [tarjeta(t) for t in mat.get("tipos_losa", [])],
        "cubiertas": [tarjeta(t) for t in mat.get("tipos_cubierta", [])],
        "marcos": mat.get("tipos_marco", []),
        "por_defecto": {"losa": mat.get("tipo_losa_por_defecto"), "cubierta": mat.get("tipo_cubierta_por_defecto"),
                        "muro": mat.get("tipo_muro_por_defecto"), "marco": mat.get("tipo_marco_por_defecto")},
        "estructura": {"sistema": est.get("sistema"), "material": est.get("material_estructural"),
                       "columnas": est.get("columnas"), "vigas": est.get("vigas")},
        "materiales": mat.get("materiales", []),
    }


# ---------------------------------------------------------------- edición del catálogo
GRUPOS = {"muros": "tipos_muro", "losas": "tipos_losa", "cubiertas": "tipos_cubierta", "marcos": "tipos_marco"}
DEFECTO = {"muros": "tipo_muro_por_defecto", "losas": "tipo_losa_por_defecto", "cubiertas": "tipo_cubierta_por_defecto",
           "marcos": "tipo_marco_por_defecto"}
REGLAS = {"muros": "reglas_muro", "marcos": "reglas_marco"}
CLAVE_PROYECTO = {"losas": "losa", "cubiertas": "cubierta"}


def _ry():
    from ruamel.yaml import YAML
    y = YAML()                     # ida y vuelta: conserva comentarios y orden del archivo
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _leer_mat_rt():
    ruta = _lineamientos() / "materialidades.yaml"
    with ruta.open(encoding="utf-8") as f:
        return ruta, _ry().load(f)


def _guardar_mat_rt(ruta: Path, datos) -> None:
    """Valida con las mismas reglas del parser y recién entonces escribe."""
    import io
    from cad2revit.config import _validar_materialidades
    plano = json.loads(json.dumps(datos))          # copia sin metadatos de ruamel
    errores = _validar_materialidades(plano)
    if errores:
        raise HTTPException(400, "El lego no es válido:\n• " + "\n• ".join(errores))
    buf = io.StringIO()
    _ry().dump(datos, buf)
    ruta.write_text(_reubicar_titulos(buf.getvalue()), encoding="utf-8")


def _reubicar_titulos(texto: str) -> str:
    """ruamel deja los títulos de sección (comentarios en columna 0) pegados al último
    elemento de la lista anterior; si quedan sobre elementos agregados, se devuelven
    justo antes de la siguiente clave de primer nivel (su sección real)."""
    lineas = texto.split("\n")
    cambiado = True
    while cambiado:
        cambiado = False
        i = 0
        while i < len(lineas):
            if lineas[i].startswith("#"):
                j = i
                while j < len(lineas) and (lineas[j].startswith("#") or lineas[j] == ""):
                    j += 1
                if j < len(lineas) and lineas[j].startswith("  - "):
                    k = j
                    while k < len(lineas) and (lineas[k].startswith(" ") or lineas[k] == ""):
                        k += 1
                    bloque = lineas[i:j]
                    while k > j and lineas[k - 1] == "":
                        k -= 1
                    lineas = lineas[:i] + lineas[j:k] + [""] + [l for l in bloque if l != "" or True] + lineas[k:]
                    cambiado = True
                    break
                i = j
            else:
                i += 1
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lineas))


def _flow(d: dict):
    """Diccionario en una línea ({funcion: ..., material: ..., espesor: ...}) como en el archivo."""
    from ruamel.yaml.comments import CommentedMap
    m = CommentedMap(d)
    m.fa.set_flow_style()
    return m


def _marco_a_yaml(lego: dict) -> dict:
    from ruamel.yaml.comments import CommentedMap
    out = CommentedMap()
    out["nombre"] = lego["nombre"].strip()
    out["sistema"] = lego.get("sistema")
    out["material"] = lego.get("material")
    if lego.get("vano_maximo"):
        out["vano_maximo"] = round(float(lego["vano_maximo"]), 2)
    roles = ["columna", "viga"] + (["diagonal"] if lego.get("sistema") == "arriostrado_x" else [])
    for rol in roles:
        p = lego.get(rol) or {}
        out[rol] = _flow({"perfil": (p.get("perfil") or "").strip(), "forma": p.get("forma"),
                          "b": round(float(p.get("b") or 0), 4), "h": round(float(p.get("h") or 0), 4),
                          "familia": (p.get("familia") or "").strip()})
    return out


def _lego_a_yaml(grupo: str, lego: dict) -> dict:
    if grupo == "marcos":
        return _marco_a_yaml(lego)
    from ruamel.yaml.comments import CommentedMap
    out = CommentedMap()
    out["nombre"] = lego["nombre"].strip()
    if grupo != "cubiertas" or lego.get("estructural"):
        out["estructural"] = bool(lego.get("estructural"))
    out["capas"] = [_flow({"funcion": c["funcion"], "material": c["material"], "espesor": round(float(c["espesor"]), 4)})
                    for c in lego.get("capas") or []]
    e = lego.get("entramado")
    if grupo == "muros" and e:
        ent = CommentedMap()
        ent["material"] = e["material"]
        ent["pie_derecho"] = _flow({"b": round(float(e["pie_derecho"]["b"]), 4), "h": round(float(e["pie_derecho"]["h"]), 4)})
        ent["separacion_pies_derechos"] = round(float(e.get("separacion_pies_derechos", 0.4)), 3)
        ent["separacion_horizontales"] = round(float(e.get("separacion_horizontales", 0.4)), 3)
        ent["soleras"] = bool(e.get("soleras", True))
        ent["refuerzo_vanos"] = bool(e.get("refuerzo_vanos", True))
        rv = e.get("revit") or {}
        r = CommentedMap()
        for k in ("familia_vertical", "familia_horizontal"):
            f = rv.get(k) or {}
            r[k] = _flow({"familia": f.get("familia") or "", "tipo": f.get("tipo") or ""})
        ent["revit"] = r
        out["entramado"] = ent
    return out


def _usos(grupo: str, nombre: str) -> list[str]:
    """Dónde se usa un lego: reglas, valores por defecto y proyectos."""
    usos = []
    mat = _leer_yaml_seguro(_lineamientos() / "materialidades.yaml")
    if grupo in REGLAS and any(r.get("tipo") == nombre for r in mat.get(REGLAS[grupo], [])):
        usos.append(f"reglas automáticas (Lineamientos → materialidades.yaml → {REGLAS[grupo]})")
    if mat.get(DEFECTO[grupo]) == nombre:
        usos.append(f"valor por defecto ({DEFECTO[grupo]})")
    for d in (_datos() / "proyectos").iterdir():
        legos = _leer_yaml_seguro(d / "entradas" / "proyecto.yaml").get("legos") or {}
        if (grupo in ("muros", "marcos") and nombre in (legos.get(grupo) or {}).values()) or \
           (grupo in CLAVE_PROYECTO and legos.get(CLAVE_PROYECTO[grupo]) == nombre):
            usos.append(f"proyecto '{d.name}'")
    return usos


def _renombrar_en_proyectos(grupo: str, viejo: str, nuevo: str) -> None:
    for d in (_datos() / "proyectos").iterdir():
        py = d / "entradas" / "proyecto.yaml"
        if not py.exists():
            continue
        datos = _leer_yaml_seguro(py)
        legos = datos.get("legos") or {}
        cambio = False
        if grupo in ("muros", "marcos"):
            for capa, t in list((legos.get(grupo) or {}).items()):
                if t == viejo:
                    legos[grupo][capa] = nuevo
                    cambio = True
        elif legos.get(CLAVE_PROYECTO[grupo]) == viejo:
            legos[CLAVE_PROYECTO[grupo]] = nuevo
            cambio = True
        if cambio:
            guardar_legos(d.name, Legos(muros=legos.get("muros") or {}, marcos=legos.get("marcos") or {},
                                        losa=legos.get("losa"), cubierta=legos.get("cubierta")))


class Lego(BaseModel):
    nombre: str
    estructural: bool = False
    capas: list[dict] = []
    entramado: dict | None = None
    # marcos de acero
    sistema: str | None = None
    material: str | None = None
    vano_maximo: float | None = None
    columna: dict | None = None
    viga: dict | None = None
    diagonal: dict | None = None


@app.post("/api/catalogo/{grupo}", status_code=201)
def crear_lego(grupo: str, lego: Lego):
    if grupo not in GRUPOS:
        raise HTTPException(404, "Grupo desconocido")
    ruta, mat = _leer_mat_rt()
    lista = mat.setdefault(GRUPOS[grupo], [])
    if any(t["nombre"] == lego.nombre.strip() for t in lista):
        raise HTTPException(409, f"Ya existe un lego llamado '{lego.nombre}'")
    lista.append(_lego_a_yaml(grupo, lego.model_dump()))
    _guardar_mat_rt(ruta, mat)
    return {"creado": lego.nombre}


@app.put("/api/catalogo/{grupo}/{nombre}")
def editar_lego(grupo: str, nombre: str, lego: Lego):
    if grupo not in GRUPOS:
        raise HTTPException(404, "Grupo desconocido")
    ruta, mat = _leer_mat_rt()
    lista = mat.get(GRUPOS[grupo]) or []
    i = next((k for k, t in enumerate(lista) if t["nombre"] == nombre), None)
    if i is None:
        raise HTTPException(404, f"No existe el lego '{nombre}'")
    nuevo = lego.nombre.strip()
    if nuevo != nombre and any(t["nombre"] == nuevo for t in lista):
        raise HTTPException(409, f"Ya existe un lego llamado '{nuevo}'")
    lista[i] = _lego_a_yaml(grupo, lego.model_dump())
    if nuevo != nombre:                       # el nombre cambia también donde se usa
        for r in mat.get(REGLAS[grupo], []) if grupo in REGLAS else []:
            if r.get("tipo") == nombre:
                r["tipo"] = nuevo
        if mat.get(DEFECTO[grupo]) == nombre:
            mat[DEFECTO[grupo]] = nuevo
    _guardar_mat_rt(ruta, mat)
    if nuevo != nombre:
        _renombrar_en_proyectos(grupo, nombre, nuevo)
    return {"guardado": nuevo}


@app.delete("/api/catalogo/{grupo}/{nombre}")
def eliminar_lego(grupo: str, nombre: str):
    if grupo not in GRUPOS:
        raise HTTPException(404, "Grupo desconocido")
    usos = _usos(grupo, nombre)
    if usos:
        raise HTTPException(409, f"No se puede eliminar '{nombre}' porque se usa en:\n• " + "\n• ".join(usos))
    ruta, mat = _leer_mat_rt()
    lista = mat.get(GRUPOS[grupo]) or []
    i = next((k for k, t in enumerate(lista) if t["nombre"] == nombre), None)
    if i is None:
        raise HTTPException(404, f"No existe el lego '{nombre}'")
    del lista[i]
    _guardar_mat_rt(ruta, mat)
    return {"eliminado": nombre}


class Material(BaseModel):
    nombre: str
    clase: str
    color: list[int]


@app.get("/api/materiales")
def listar_materiales():
    return _leer_yaml_seguro(_lineamientos() / "materialidades.yaml").get("materiales", [])


@app.post("/api/materiales", status_code=201)
def crear_material(m: Material):
    ruta, mat = _leer_mat_rt()
    lista = mat.setdefault("materiales", [])
    if any(x["nombre"] == m.nombre.strip() for x in lista):
        raise HTTPException(409, f"Ya existe el material '{m.nombre}'")
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    nuevo = CommentedMap()
    nuevo["nombre"] = m.nombre.strip()
    nuevo["clase"] = m.clase
    col = CommentedSeq([max(0, min(255, int(c))) for c in m.color[:3]])
    col.fa.set_flow_style()
    nuevo["color"] = col
    lista.append(nuevo)
    _guardar_mat_rt(ruta, mat)
    return {"creado": m.nombre}


def _capas_de_muro(d: Path, categoria: str = "muros") -> list[str]:
    """Capas del CAD de una categoría (muros, marcos) leídas de los planos del proyecto."""
    capas_cfg = _leer_yaml_seguro(_lineamientos() / "capas_cad.yaml").get("capas") or {}
    patron = re.compile(capas_cfg[categoria]) if capas_cfg.get(categoria) else None
    py = _leer_yaml_seguro(d / "entradas" / "proyecto.yaml")
    archivos = [Path(p["archivo"]).name for p in py.get("plantas") or []] or \
               [f.name for f in (d / "planos").glob("*.dxf")]
    capas = set()
    for nombre in archivos:
        f = d / "planos" / nombre
        if not f.exists():
            continue
        try:
            capas |= {sg.capa for sg in leer(str(f)).segmentos if patron and patron.search(sg.capa)}
        except Exception:
            pass
    return sorted(capas)


class Legos(BaseModel):
    muros: dict[str, str | None] = {}
    marcos: dict[str, str | None] = {}
    losa: str | None = None
    cubierta: str | None = None


@app.get("/api/proyectos/{pid}/legos")
def leer_legos(pid: str):
    d = _proyecto(pid)
    legos = _leer_yaml_seguro(d / "entradas" / "proyecto.yaml").get("legos") or {}
    return {"capas_muro": _capas_de_muro(d), "muros": legos.get("muros") or {},
            "capas_marco": _capas_de_muro(d, "marcos"), "marcos": legos.get("marcos") or {},
            "losa": legos.get("losa"), "cubierta": legos.get("cubierta")}


@app.put("/api/proyectos/{pid}/legos")
def guardar_legos(pid: str, datos: Legos):
    """Guarda en proyecto.yaml qué lego se usa en cada capa de muros, y la losa y cubierta."""
    d = _proyecto(pid)
    py = d / "entradas" / "proyecto.yaml"
    texto = py.read_text(encoding="utf-8")
    texto = re.sub(r"^# Legos elegidos.*\n?", "", texto, flags=re.M)
    texto = re.sub(r"^legos:.*?(?=^\S|\Z)", "", texto, flags=re.M | re.S).rstrip() + "\n"
    bloque = {"muros": {k: v for k, v in datos.muros.items() if v}}
    if any(datos.marcos.values()):
        bloque["marcos"] = {k: v for k, v in datos.marcos.items() if v}
    if datos.losa:
        bloque["losa"] = datos.losa
    if datos.cubierta:
        bloque["cubierta"] = datos.cubierta
    texto += "\n# Legos elegidos para este proyecto (se editan en la pestaña Legos de la app)\n"
    texto += yaml.safe_dump({"legos": bloque}, allow_unicode=True, sort_keys=False)
    py.write_text(texto, encoding="utf-8")
    return {"guardado": True, "errores": _validar_entradas(d)}


# ---------------------------------------------------------------- planos
@app.get("/api/proyectos/{pid}/planos")
def listar_planos(pid: str):
    d = _proyecto(pid)
    out = []
    for f in sorted((d / "planos").glob("*.dxf")):
        try:
            dib = leer(str(f))
            capas = sorted({s.capa for s in dib.segmentos} | {i.capa for i in dib.inserciones}
                           | {t.capa for t in dib.textos})
            bloques = sorted({i.nombre for i in dib.inserciones})
            err = None
        except Exception as e:
            capas, bloques, err = [], [], f"No se pudo leer: {e}"
        out.append({"nombre": f.name, "bytes": f.stat().st_size, "capas": capas, "bloques": bloques,
                    "ruta_en_proyecto": f"../planos/{f.name}", "error": err})
    return out


@app.post("/api/proyectos/{pid}/planos", status_code=201)
async def subir_planos(pid: str, archivos: list[UploadFile] = File(...)):
    d = _proyecto(pid)
    guardados = []
    for up in archivos:
        nombre = _seguro(Path(up.filename or "").name, "nombre de archivo")
        ext = Path(nombre).suffix.lower()
        if ext == ".dwg":
            raise HTTPException(400, f"{nombre}: los DWG deben convertirse a DXF antes de subirlos "
                                     "(AutoCAD: GUARDARCOMO → DXF, o ODA File Converter)")
        if ext != ".dxf":
            raise HTTPException(400, f"{nombre}: solo se aceptan archivos .dxf")
        contenido = await up.read()
        if len(contenido) > MAX_SUBIDA:
            raise HTTPException(413, f"{nombre}: supera {MAX_SUBIDA // 1024 // 1024} MB")
        (d / "planos" / nombre).write_bytes(contenido)
        guardados.append(nombre)
    return {"guardados": guardados}


@app.delete("/api/proyectos/{pid}/planos/{nombre}", status_code=204)
def borrar_plano(pid: str, nombre: str):
    f = _proyecto(pid) / "planos" / _seguro(nombre)
    if not f.exists():
        raise HTTPException(404, "No existe el plano")
    f.unlink()


@app.get("/api/proyectos/{pid}/planos/{nombre}/geometria")
def geometria_plano(pid: str, nombre: str):
    """Líneas del CAD (en metros) para dibujar el plano de fondo en la vista 2D."""
    f = _proyecto(pid) / "planos" / _seguro(nombre)
    if not f.exists():
        raise HTTPException(404, "No existe el plano")
    dib = leer(str(f))
    segs = dib.segmentos[:MAX_SEGMENTOS_VISTA]
    return {
        "segmentos": [[s.a[0], s.a[1], s.b[0], s.b[1], s.capa] for s in segs],
        "textos": [[t.punto[0], t.punto[1], t.texto, t.capa] for t in dib.textos[:2000]],
        "recortado": len(dib.segmentos) > MAX_SEGMENTOS_VISTA,
    }


# ---------------------------------------------------------------- corridas
class NuevaCorrida(BaseModel):
    notas: str = ""


class Notas(BaseModel):
    notas: str


def _conteos(m: dict) -> dict:
    f = m["fundaciones"]
    return {
        "niveles": len(m["niveles"]), "ejes": len(m["ejes"]), "muros": len(m["muros"]),
        "muros_estructurales": sum(x["estructural"] for x in m["muros"]),
        "puertas": sum(a["categoria"] == "puerta" for a in m["aberturas"]),
        "ventanas": sum(a["categoria"] == "ventana" for a in m["aberturas"]),
        "losas": len(m["losas"]), "cielos": len(m.get("cielos", [])), "columnas": len(m["columnas"]),
        "vigas": len(m["vigas"]), "fundaciones": len(f["corridas"]) + len(f["aisladas"]),
        "recintos": len(m["recintos"]), "advertencias": len(m["advertencias"]),
        "piezas_entramado": sum(len(e["piezas"]) for e in m.get("entramados", [])),
        "marcos": len(m.get("marcos", [])),
        "perfiles_acero": sum(len(f["columnas"]) + len(f["vigas"]) + len(f["diagonales"]) for f in m.get("marcos", [])),
    }


@app.post("/api/proyectos/{pid}/corridas", status_code=201)
def ejecutar(pid: str, datos: NuevaCorrida):
    d = _proyecto(pid)
    previas = sorted(int(c.name) for c in (d / "corridas").glob("[0-9]*"))
    n = (previas[-1] + 1) if previas else 1
    c = d / "corridas" / f"{n:04d}"
    # copia de lo usado en esta corrida (para el historial: comparar / restaurar)
    (c / "entradas").mkdir(parents=True)
    for a in ARCHIVOS_ENTRADA:
        shutil.copy2(_ruta_entrada(d, a), c / "entradas" / a)
    resumen = {"n": n, "fecha": _ahora(), "notas": datos.notas, "ok": False, "error": None,
               "conteos": None, "lod": None, "errores_esquema": []}
    try:
        modelo = construir_modelo(cargar(d / "entradas" / "proyecto.yaml"))
        resumen["errores_esquema"] = validar(modelo)
        resumen["ok"] = not resumen["errores_esquema"]
        resumen["conteos"] = _conteos(modelo)
        resumen["lod"] = modelo["lod"]["preset"]
        (c / "modelo.json").write_text(json.dumps(modelo, ensure_ascii=False, indent=1), encoding="utf-8")
    except ErrorConfiguracion as e:
        resumen["error"] = str(e)
    except Exception as e:
        resumen["error"] = f"Error inesperado al procesar: {type(e).__name__}: {e}"
        resumen["traza"] = traceback.format_exc(limit=5)
    (c / "resumen.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
    return resumen


@app.get("/api/proyectos/{pid}/corridas")
def listar_corridas(pid: str):
    d = _proyecto(pid)
    return [json.loads((c / "resumen.json").read_text(encoding="utf-8"))
            for c in sorted((d / "corridas").glob("[0-9]*"), reverse=True) if (c / "resumen.json").exists()]


@app.get("/api/proyectos/{pid}/corridas/{n}")
def ver_corrida(pid: str, n: int):
    d = _proyecto(pid)
    c = _corrida_dir(d, n)
    resumen = json.loads((c / "resumen.json").read_text(encoding="utf-8"))
    modelo = json.loads((c / "modelo.json").read_text(encoding="utf-8")) if (c / "modelo.json").exists() else None
    plantas = {}
    try:
        py = yaml.safe_load((c / "entradas" / "proyecto.yaml").read_text(encoding="utf-8")) or {}
        plantas = {p["nivel"]: Path(p["archivo"]).name for p in py.get("plantas") or []}
    except Exception:
        pass
    return {"resumen": resumen, "modelo": modelo, "plantas": plantas}


@app.get("/api/proyectos/{pid}/corridas/{n}/modelo.json")
def descargar_modelo(pid: str, n: int):
    d = _proyecto(pid)
    f = _corrida_dir(d, n) / "modelo.json"
    if not f.exists():
        raise HTTPException(404, "Esta corrida no generó modelo (tuvo errores)")
    return FileResponse(f, media_type="application/json", filename=f"{pid}-corrida{n}-modelo.json")


@app.put("/api/proyectos/{pid}/corridas/{n}/notas")
def guardar_notas(pid: str, n: int, datos: Notas):
    c = _corrida_dir(_proyecto(pid), n)
    r = json.loads((c / "resumen.json").read_text(encoding="utf-8"))
    r["notas"] = datos.notas
    (c / "resumen.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    return r


@app.post("/api/proyectos/{pid}/corridas/{n}/restaurar")
def restaurar(pid: str, n: int):
    """Vuelve los lineamientos actuales a los que se usaron en la corrida n."""
    d = _proyecto(pid)
    c = _corrida_dir(d, n)
    for a in ARCHIVOS_ENTRADA:
        shutil.copy2(c / "entradas" / a, _ruta_entrada(d, a))
    _enlazar_lineamientos(d)
    return {"restaurado": n, "errores": _validar_entradas(d)}


@app.get("/api/proyectos/{pid}/corridas/{a}/comparar/{b}")
def comparar(pid: str, a: int, b: int):
    d = _proyecto(pid)
    ca, cb = _corrida_dir(d, a), _corrida_dir(d, b)
    difs = {}
    for f in ARCHIVOS_ENTRADA:
        la = (ca / "entradas" / f).read_text(encoding="utf-8").splitlines()
        lb = (cb / "entradas" / f).read_text(encoding="utf-8").splitlines()
        dif = list(difflib.unified_diff(la, lb, f"corrida {a}", f"corrida {b}", lineterm="", n=2))
        if dif:
            difs[f] = "\n".join(dif)
    ra = json.loads((ca / "resumen.json").read_text(encoding="utf-8"))
    rb = json.loads((cb / "resumen.json").read_text(encoding="utf-8"))
    claves = sorted(set((ra.get("conteos") or {})) | set((rb.get("conteos") or {})))
    conteos = {k: [(ra.get("conteos") or {}).get(k), (rb.get("conteos") or {}).get(k)] for k in claves}
    return {"diferencias": difs, "conteos": conteos}


@app.exception_handler(ErrorConfiguracion)
def _error_cfg(_, exc: ErrorConfiguracion):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# ---------------------------------------------------------------- interfaz
app.mount("/", StaticFiles(directory=RAIZ / "app" / "static", html=True), name="static")
