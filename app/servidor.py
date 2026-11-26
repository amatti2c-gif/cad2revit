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

import os as _os
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    _os.environ.setdefault(_v, "1")          # antes de cargar numpy: un hilo de álgebra lineal (ver Dockerfile)

import base64
import binascii
import difflib
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
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


def _convertidor_dwg() -> str | None:
    """Ruta del convertidor DWG→DXF (LibreDWG, instalado en la imagen de Railway)."""
    return shutil.which("dwg2dxf")


def _convertir_dwg(origen: Path, destino: Path) -> None:
    """Convierte un DWG a DXF (formato de trabajo de la app). Lanza HTTPException si falla."""
    import subprocess
    exe = _convertidor_dwg()
    if not exe:
        raise HTTPException(400, f"{origen.name}: este servidor todavía no tiene el convertidor de DWG instalado. "
                                 "Sube el plano como DXF (AutoCAD: GUARDARCOMO → DXF) o espera a que se publique "
                                 "la versión con soporte DWG.")
    try:
        r = subprocess.run([exe, "-y", "-o", str(destino), str(origen)], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise HTTPException(400, f"{origen.name}: la conversión DWG→DXF tardó demasiado")
    if r.returncode != 0 or not destino.exists() or destino.stat().st_size == 0:
        detalle = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        raise HTTPException(400, f"{origen.name}: no se pudo convertir el DWG. " + " ".join(detalle))


@app.get("/salud")
def salud():
    # Railway informa el commit desplegado; sirve para saber qué versión está en línea
    commit = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("GIT_COMMIT") or "")[:7]
    return {"ok": True, "version": __version__, "commit": commit or "local",
            "tipos_madera": "entramado" in (RAIZ / "entradas" / "materialidades.yaml").read_text(encoding="utf-8"),
            "dwg": bool(_convertidor_dwg())}


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
    if str(d) not in _COMPLETADOS:
        _COMPLETADOS.add(str(d))
        try:
            _completar_lineamientos(d)
        except Exception:          # nunca impedir que la app arranque por esto
            traceback.print_exc()
    return d


_COMPLETADOS: set[str] = set()


def _completar_lineamientos(d: Path) -> list[str]:
    """Los lineamientos comunes se copian una sola vez; cuando la app trae secciones nuevas (p. ej. los
    legos de marco) se AGREGAN sin tocar lo que el usuario ya editó: secciones y claves que faltan, y
    los materiales que esas secciones nuevas necesitan."""
    import io
    agregados = []
    y = _ry()
    for a in LINEAMIENTOS:
        ruta, base = d / a, RAIZ / "entradas" / a
        if not ruta.exists():
            shutil.copy2(base, ruta)
            continue
        with ruta.open(encoding="utf-8") as f:
            actual = y.load(f)
        with base.open(encoding="utf-8") as f:
            defecto = y.load(f)
        if not isinstance(actual, dict) or not isinstance(defecto, dict):
            continue
        nuevas = []
        for clave, valor in defecto.items():
            if clave not in actual:
                actual[clave] = valor
                nuevas.append(clave)
                agregados.append(f"{a}: {clave}")
            elif isinstance(valor, dict) and isinstance(actual[clave], dict):
                for sub, v in valor.items():
                    if sub not in actual[clave]:
                        actual[clave][sub] = v
                        agregados.append(f"{a}: {clave}.{sub}")
        if a == "materialidades.yaml" and nuevas:
            # materiales que usan las secciones nuevas (p. ej. el acero de los marcos)
            usados = set(re.findall(r"material:\s*([^\n,}]+)", json.dumps(
                {k: json.loads(json.dumps(actual[k])) for k in nuevas}, ensure_ascii=False).replace('"', "")))
            tiene = {m.get("nombre") for m in actual.get("materiales") or []}
            for m in defecto.get("materiales") or []:
                if m.get("nombre") in usados - tiene:
                    actual["materiales"].append(m)
                    agregados.append(f"{a}: material {m.get('nombre')}")
        if agregados and any(x.startswith(a) for x in agregados):
            buf = io.StringIO()
            y.dump(actual, buf)
            _escribir(ruta, buf.getvalue())
    if agregados:
        (d / "actualizaciones.txt").open("a", encoding="utf-8").write(
            f"{_ahora()}: se agregó de los valores por defecto: " + "; ".join(agregados) + "\n")
    return agregados


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
        _escribir(py, texto)
    for a in LINEAMIENTOS:                       # las copias antiguas ya no se usan
        (d / "entradas" / a).unlink(missing_ok=True)


def _texto_proyecto_inicial(nombre: str) -> str:
    texto = (RAIZ / "entradas" / "proyecto.yaml").read_text(encoding="utf-8").replace("../ejemplos/", "../planos/")
    return re.sub(r"^nombre:.*$", f"nombre: {json.dumps(nombre, ensure_ascii=False)}", texto, count=1, flags=re.M)


def _escribir(ruta: Path, texto: str) -> None:
    """Escritura atómica: se escribe a un temporal y se reemplaza de una vez. Así otra consulta que lee
    el archivo al mismo tiempo nunca lo ve vacío o a medias (eso hacía que proyecto.yaml se «reparara»
    con la plantilla de ejemplo)."""
    ruta = Path(ruta)
    tmp = ruta.with_name(f".{ruta.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(texto, encoding="utf-8")
    os.replace(tmp, ruta)


def _ultima_entrada_valida(d: Path) -> str | None:
    """proyecto.yaml de la corrida más reciente que tiene niveles y cuyos planos existen."""
    corridas = sorted((d / "corridas").glob("[0-9]*"), key=lambda c: int(c.name) if c.name.isdigit() else -1, reverse=True)
    for c in corridas:
        f = c / "entradas" / "proyecto.yaml"
        if not f.exists():
            continue
        datos = _leer_yaml_seguro(f)
        planos = [p.get("archivo") for p in datos.get("plantas") or [] if isinstance(p, dict)]
        if datos.get("niveles") and planos and all((d / "entradas" / x).resolve().exists() for x in planos if x):
            return f.read_text(encoding="utf-8")
    return None


def _reparar_proyecto(d: Path) -> None:
    """Si proyecto.yaml perdió sus niveles (archivo dañado), se recupera de la última corrida válida (o, si
    no hay, de la plantilla conservando los legos). Antes se reintenta la lectura y se guarda una copia."""
    py = d / "entradas" / "proyecto.yaml"
    marca = d / "reparado.txt"
    datos = _leer_yaml_seguro(py) if py.exists() else {}
    # proyecto reparado antes con la plantilla (versión anterior): apunta a planos de ejemplo que no existen
    if datos.get("niveles") and marca.exists():
        planos = [p.get("archivo") for p in datos.get("plantas") or [] if isinstance(p, dict)]
        if planos and not all((d / "entradas" / x).resolve().exists() for x in planos if x):
            bueno = _ultima_entrada_valida(d)
            if bueno:
                _escribir(d / "entradas" / f"proyecto.dañado-{_ahora().replace(':', '-')}.yaml", py.read_text(encoding="utf-8"))
                _escribir(py, bueno)
                marca.rename(d / "reparado-atendido.txt")
            return
    if datos.get("niveles"):
        return
    for _ in range(3):                      # quizá otra consulta lo estaba escribiendo: reintentar
        time.sleep(0.15)
        datos = _leer_yaml_seguro(py) if py.exists() else {}
        if datos.get("niveles"):
            return
    if py.exists() and py.read_text(encoding="utf-8").strip():
        _escribir(d / "entradas" / f"proyecto.dañado-{_ahora().replace(':', '-')}.yaml", py.read_text(encoding="utf-8"))
    bueno = _ultima_entrada_valida(d)
    if bueno:
        py.parent.mkdir(parents=True, exist_ok=True)
        _escribir(py, bueno)
        _escribir(d / "reparado-atendido.txt", f"{_ahora()}: proyecto.yaml dañado; recuperado de la última corrida válida\n")
        return
    _reparar_desde_plantilla(d, py, datos)


def _reparar_desde_plantilla(d: Path, py: Path, datos: dict) -> None:
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
    _escribir(py, texto)
    _escribir(d / "reparado.txt", f"{_ahora()}: proyecto.yaml sin niveles; reconstruido desde la plantilla\n")


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
    _escribir(py, _texto_proyecto_inicial(nombre))
    if datos.con_ejemplo:
        from generar_ejemplo import generar
        generar(d / "planos")
    _enlazar_lineamientos(d)
    meta = {"id": pid, "nombre": nombre, "creado": _ahora(), "con_ejemplo": datos.con_ejemplo}
    _escribir((d / "meta.json"), json.dumps(meta, ensure_ascii=False, indent=2))
    return meta


def _papelera() -> Path:
    d = _datos() / "papelera"
    d.mkdir(exist_ok=True)
    return d


@app.delete("/api/proyectos/{pid}", status_code=204)
def borrar_proyecto(pid: str):
    """Envía el proyecto a la papelera (se puede restaurar o borrar definitivamente desde ahí)."""
    d = _proyecto(pid)
    marca = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    shutil.move(str(d), str(_papelera() / f"{d.name}__{marca}"))


@app.get("/api/papelera")
def listar_papelera():
    out = []
    for d in sorted(_papelera().iterdir(), reverse=True):
        if not (d / "meta.json").is_file():
            continue
        try:
            m = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            m = {}
        pid, _, marca = d.name.rpartition("__")
        try:
            fecha = datetime.strptime(marca, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            fecha = None
        out.append({"item": d.name, "id": pid or d.name, "nombre": m.get("nombre") or pid, "eliminado": fecha})
    return out


def _item_papelera(item: str) -> Path:
    d = _papelera() / item
    if not re.fullmatch(r"[\w.\-]+", item) or item.startswith(".") or not (d / "meta.json").is_file():
        raise HTTPException(404, "No está en la papelera")
    return d


@app.post("/api/papelera/{item}/restaurar")
def restaurar_proyecto(item: str):
    d = _item_papelera(item)
    pid = item.rpartition("__")[0] or item
    destino, k = _datos() / "proyectos" / pid, 1
    while destino.exists():
        k += 1
        destino = _datos() / "proyectos" / f"{pid}-{k}"
    shutil.move(str(d), str(destino))
    return {"id": destino.name}


@app.delete("/api/papelera/{item}", status_code=204)
def borrar_definitivo(item: str):
    shutil.rmtree(_item_papelera(item))


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
    _escribir(_ruta_entrada(d, archivo), datos.texto)
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
    _escribir(ruta, _reubicar_titulos(buf.getvalue()))


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
    out["techo"] = lego.get("techo") or "horizontal"          # horizontal | dos_aguas
    if out["techo"] == "dos_aguas":
        if lego.get("pendiente"):
            out["pendiente"] = round(float(lego["pendiente"]), 1)
        out["pilar_central"] = bool(lego.get("pilar_central"))      # columna bajo la cumbrera o vano libre
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
    techo: str | None = None                 # marcos: horizontal | dos_aguas
    pendiente: float | None = None           # marcos a dos aguas sin techo inclinado (grados)
    pilar_central: bool | None = None        # marcos a dos aguas: columna bajo la cumbrera
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
    texto = re.sub(r"^legos:.*?(?=^[^\s-]|\Z)", "", texto, flags=re.M | re.S).rstrip() + "\n"
    bloque = {"muros": {k: v for k, v in datos.muros.items() if v}}
    if any(datos.marcos.values()):
        bloque["marcos"] = {k: v for k, v in datos.marcos.items() if v}
    if datos.losa:
        bloque["losa"] = datos.losa
    if datos.cubierta:
        bloque["cubierta"] = datos.cubierta
    texto += "\n# Legos elegidos para este proyecto (se editan en la pestaña Legos de la app)\n"
    texto += yaml.safe_dump({"legos": bloque}, allow_unicode=True, sort_keys=False)
    _escribir(py, texto)
    return {"guardado": True, "errores": _validar_entradas(d)}


# ---------------------------------------------------------------- estructuración por tramos
OPCIONES_TRAMO = {"auto", "muro", "momento", "arriostrado", "ninguno"}


class EleccionesTramos(BaseModel):
    elecciones: dict[str, str] = {}


def _fijar_ejes(d: Path) -> bool:
    """Guarda en proyecto.yaml (ejes_base) los ejes de la última corrida correcta, antes de las ediciones.
    Los ejes generados desde los muros cambian si cambian las capas o el plano; fijados, las elecciones de
    tramos y las ediciones siguen calzando. Devuelve True si los fijó ahora."""
    py = d / "entradas" / "proyecto.yaml"
    datos = _leer_yaml_seguro(py)
    if datos.get("ejes_base"):
        return False
    for c in sorted((d / "corridas").glob("[0-9]*"), key=lambda c: int(c.name) if c.name.isdigit() else -1, reverse=True):
        r = c / "resumen.json"
        if not r.exists() or not json.loads(r.read_text(encoding="utf-8")).get("ok"):
            continue
        m = json.loads((c / "modelo.json").read_text(encoding="utf-8"))
        ejes = m.get("ejes_generados") or ([] if datos.get("ejes_usuario") else m.get("ejes"))
        if not ejes:
            return False
        base = [{"nombre": e["nombre"], "inicio": [round(v, 4) for v in e["inicio"]], "fin": [round(v, 4) for v in e["fin"]],
                 "origen": "fijado"} for e in ejes]
        _escribir(py, _reemplazar_bloque(py.read_text(encoding="utf-8"), "ejes_base", base))
        return True
    return False


@app.post("/api/proyectos/{pid}/ejes/regenerar")
def regenerar_ejes(pid: str):
    """Vuelve a generar los ejes desde el plano (se quitan los fijados). Las ediciones pueden dejar de calzar."""
    d = _proyecto(pid)
    py = d / "entradas" / "proyecto.yaml"
    _escribir(py, _reemplazar_bloque(py.read_text(encoding="utf-8"), "ejes_base", None))
    return {"ok": True}


class Ediciones(BaseModel):
    ejes: dict[str, dict | None] | None = None      # ejes_usuario (por nombre de eje)
    muros: list[dict] | None = None                  # muros_usuario (dibujados en la app)
    quitar: list[dict] | None = None                 # muros_quitar (muros del plano quitados en la app)
    marcos: list[dict] | None = None                 # marcos_usuario (marcos dibujados en la app)
    editar: list[dict] | None = None                 # muros_editar (muros del plano con otro lego / carácter)
    buhardilla: bool | None = None                   # nivel buhardilla: completar los muros hasta la cubierta
    cotas: dict | None = None                        # cotas_ejes: {verticales: {eje: m}, horizontales: {eje: m}}


def _reajuste_de(d: Path):
    """Reajuste de las cotas de ejes del proyecto (None si no tiene cotas)."""
    from cad2revit import cotas as cotas_mod
    py = _leer_yaml_seguro(d / "entradas" / "proyecto.yaml")
    if not py.get("cotas_ejes"):
        return None
    try:
        return cotas_mod.para_proyecto(cargar(d / "entradas" / "proyecto.yaml"))
    except Exception:
        return None


@app.get("/api/proyectos/{pid}/ediciones")
def leer_ediciones(pid: str):
    d = _proyecto(pid)
    py = _leer_yaml_seguro(d / "entradas" / "proyecto.yaml")
    ed = {"ejes": py.get("ejes_usuario") or {}, "muros": py.get("muros_usuario") or [],
          "quitar": py.get("muros_quitar") or [], "marcos": py.get("marcos_usuario") or [],
          "editar": py.get("muros_editar") or [], "buhardilla": bool(py.get("buhardilla")),
          "cotas": py.get("cotas_ejes") or {}}
    # con cotas de ejes, la app trabaja sobre el modelo reajustado: las ediciones se muestran reajustadas
    reaj = _reajuste_de(d)
    if reaj:
        from cad2revit import cotas as cotas_mod
        ed = cotas_mod.convertir_ediciones(ed, reaj.p)
    return ed


@app.put("/api/proyectos/{pid}/ediciones")
def guardar_ediciones(pid: str, datos: Ediciones):
    """Ejes editados y muros dibujados en la pestaña Estructuración (se aplican sobre lo leído del plano)."""
    d = _proyecto(pid)
    _fijar_ejes(d)
    py = d / "entradas" / "proyecto.yaml"
    niveles = {n.get("nombre") for n in _leer_yaml_seguro(py).get("niveles") or []}
    # con cotas de ejes, lo que llega está en el modelo reajustado: se guarda en las coordenadas del plano original
    reaj = _reajuste_de(d)
    if reaj:
        from cad2revit import cotas as cotas_mod
        conv = cotas_mod.convertir_ediciones({"muros": datos.muros, "quitar": datos.quitar, "marcos": datos.marcos,
                                              "editar": datos.editar}, reaj.p_inv)
        for k, campo in (("muros", "muros"), ("quitar", "quitar"), ("marcos", "marcos"), ("editar", "editar")):
            if getattr(datos, campo) is not None:
                setattr(datos, campo, conv[k])
        if datos.ejes:
            inv_x = lambda v: cotas_mod.Reajuste._f([(b, a) for a, b in reaj.px], v)       # noqa: E731
            inv_y = lambda v: cotas_mod.Reajuste._f([(b, a) for a, b in reaj.py], v)       # noqa: E731
            for c in datos.ejes.values():
                if not c:
                    continue
                for k in ("inicio", "fin"):
                    if isinstance(c.get(k), list) and len(c[k]) == 2:
                        c[k] = [round(inv_x(c[k][0]), 4), round(inv_y(c[k][1]), 4)]
    texto = py.read_text(encoding="utf-8")
    if datos.cotas is not None:
        limpias = {}
        for g in ("verticales", "horizontales"):
            v = {str(k): round(float(x), 4) for k, x in ((datos.cotas or {}).get(g) or {}).items()}
            if v:
                limpias[g] = v
        texto = _reemplazar_bloque(texto, "cotas_ejes", limpias)
    if datos.ejes is not None:
        for nombre, c in datos.ejes.items():
            if c is None:
                continue
            for k in ("inicio", "fin"):
                if k in c and not (isinstance(c[k], list) and len(c[k]) == 2):
                    raise HTTPException(400, f"Eje {nombre}: {k} debe ser [x, y]")
        texto = _reemplazar_bloque(texto, "ejes_usuario", datos.ejes or {})
    if datos.muros is not None:
        for i, w in enumerate(datos.muros, 1):
            if w.get("nivel") not in niveles:
                raise HTTPException(400, f"Muro dibujado {i}: el nivel '{w.get('nivel')}' no existe")
            if not all(isinstance(w.get(k), list) and len(w[k]) == 2 for k in ("inicio", "fin")):
                raise HTTPException(400, f"Muro dibujado {i}: inicio y fin deben ser [x, y]")
        limpios = [{"nivel": w["nivel"], "inicio": [round(float(v), 4) for v in w["inicio"]],
                    "fin": [round(float(v), 4) for v in w["fin"]], "tipo": w.get("tipo"),
                    "estructural": bool(w.get("estructural"))} for w in datos.muros]
        texto = _reemplazar_bloque(texto, "muros_usuario", limpios)
    if datos.quitar is not None:
        q = [{"nivel": w.get("nivel"), "inicio": [round(float(v), 4) for v in w["inicio"]],
              "fin": [round(float(v), 4) for v in w["fin"]], **({"id": w["id"]} if w.get("id") else {})}
             for w in datos.quitar if isinstance(w.get("inicio"), list) and isinstance(w.get("fin"), list)]
        texto = _reemplazar_bloque(texto, "muros_quitar", q)
    if datos.buhardilla is not None:
        texto = _reemplazar_bloque(texto, "buhardilla", bool(datos.buhardilla))
    if datos.editar is not None:
        texto = _reemplazar_bloque(texto, "muros_editar", [
            {"nivel": w.get("nivel"), "inicio": [round(float(v), 4) for v in w["inicio"]],
             "fin": [round(float(v), 4) for v in w["fin"]], "tipo": w.get("tipo"),
             "estructural": bool(w.get("estructural")), **({"id": w["id"]} if w.get("id") else {})}
            for w in datos.editar if isinstance(w.get("inicio"), list) and isinstance(w.get("fin"), list)])
    if datos.marcos is not None:
        for i, f in enumerate(datos.marcos, 1):
            if f.get("nivel") not in niveles:
                raise HTTPException(400, f"Marco dibujado {i}: el nivel '{f.get('nivel')}' no existe")
            if not all(isinstance(f.get(k), list) and len(f[k]) == 2 for k in ("inicio", "fin")) or not f.get("tipo"):
                raise HTTPException(400, f"Marco dibujado {i}: necesita inicio, fin y lego de marco")
        texto = _reemplazar_bloque(texto, "marcos_usuario", [
            {"nivel": f["nivel"], "inicio": [round(float(v), 4) for v in f["inicio"]],
             "fin": [round(float(v), 4) for v in f["fin"]], "tipo": f["tipo"]} for f in datos.marcos])
    _escribir(py, texto)
    return {"guardado": True, "errores": _validar_entradas(d)}


@app.get("/api/proyectos/{pid}/tramos")
def leer_tramos(pid: str):
    d = _proyecto(pid)
    return {"elecciones": _leer_yaml_seguro(d / "entradas" / "proyecto.yaml").get("tramos") or {}}


@app.put("/api/proyectos/{pid}/tramos")
def guardar_tramos(pid: str, datos: EleccionesTramos):
    """Guarda en proyecto.yaml las tipologías elegidas por tramo de eje ("A:1-2") o por tramo
    y piso ("A:1-2@N2"). "auto" (o no estar) = usar la propuesta de la app."""
    d = _proyecto(pid)
    _fijar_ejes(d)
    from cad2revit.tramos import valida
    tipos = {t.get("nombre") for t in _leer_yaml_seguro(_lineamientos() / "materialidades.yaml").get("tipos_muro") or []}
    malas = [f"{k}: {v}" for k, v in datos.elecciones.items() if not valida(v, tipos)]
    if malas:
        raise HTTPException(400, "Opciones no válidas: " + ", ".join(malas))
    py = d / "entradas" / "proyecto.yaml"
    texto = py.read_text(encoding="utf-8")
    texto = re.sub(r"^# Tipologías elegidas por tramo.*\n?", "", texto, flags=re.M)
    texto = re.sub(r"^tramos:.*?(?=^[^\s-]|\Z)", "", texto, flags=re.M | re.S).rstrip() + "\n"
    elegidas = {k: v for k, v in sorted(datos.elecciones.items()) if v != "auto"}
    if elegidas:
        texto += "\n# Tipologías elegidas por tramo de eje (se editan en la pestaña Estructuración de la app)\n"
        texto += yaml.safe_dump({"tramos": elegidas}, allow_unicode=True, sort_keys=False)
    _escribir(py, texto)
    return {"guardado": True, "errores": _validar_entradas(d)}


# ---------------------------------------------------------------- niveles y uso de cada plano
def _reemplazar_bloque(texto: str, clave: str, valor) -> str:
    """Reemplaza (en su lugar) el bloque de primer nivel `clave:` de un YAML, conservando el resto."""
    if isinstance(valor, (dict, list)) and valor:
        nuevo = yaml.safe_dump({clave: valor}, allow_unicode=True, sort_keys=False, default_flow_style=None)
    else:   # vacío o escalar: siempre "clave: valor" (no "{clave: valor}", que no se reconocería después)
        escalar = yaml.safe_dump(valor, allow_unicode=True, default_flow_style=True).split("\n")[0]
        nuevo = f"{clave}: {escalar}\n"
    patron = re.compile(rf"^{re.escape(clave)}:.*?(?=^[^\s-]|\Z)", flags=re.M | re.S)
    if patron.search(texto):
        return patron.sub(lambda _m: nuevo + "\n", texto, count=1)
    return texto.rstrip() + "\n\n" + nuevo


_CACHE_VISTAS: dict[tuple, list[dict]] = {}


def _vistas_de(d: Path, nombre: str) -> list[dict]:
    """Vistas (planta, fachadas, cortes…) dibujadas lado a lado dentro de un plano."""
    from cad2revit import vistas as vistas_mod
    from cad2revit.aberturas import clasificar_inserciones
    f = d / "planos" / nombre
    if not f.exists():
        return []
    st = f.stat()
    try:
        cfg = cargar(d / "entradas" / "proyecto.yaml")
    except Exception:
        cfg = None
    clave = (str(f), st.st_size, st.st_mtime_ns, json.dumps((cfg.proyecto.get("capas") if cfg else None), sort_keys=True))
    if clave not in _CACHE_VISTAS:
        dib = leer(str(f))
        if cfg:
            es_abertura = lambda i: bool(clasificar_inserciones([i], cfg)[0])  # noqa: E731
            patron_nivel, patron_muros, det = cfg.capas_cad.get("patron_nivel"), cfg.capa("muros"), cfg.deteccion
        else:
            es_abertura, patron_nivel, patron_muros, det = None, None, None, None
        _CACHE_VISTAS[clave] = vistas_mod.detectar(dib, es_abertura, patron_nivel, patron_muros, det)
    return _CACHE_VISTAS[clave]


def _k_de_caja(vistas: list[dict], caja) -> int | None:
    for v in vistas:
        if all(abs(a - b) <= 0.05 for a, b in zip(v["caja"], caja)):
            return v["k"]
    return None


def _uso_de_planos(py: dict, d: Path | None = None) -> dict[str, str]:
    """archivo -> uso del plano completo; si se usan vistas sueltas: archivo -> "vistas" y archivo#k -> uso."""
    from cad2revit.vistas import archivo_de, zonas_de
    uso = {}
    entradas = [(p, f"planta:{p['nivel']}") for p in py.get("plantas") or []] + \
               [(e, "elevacion") for e in py.get("elevaciones") or []] + \
               ([(py["techo"], "techo")] if py.get("techo") else [])
    for e, u in entradas:
        nombre = Path(archivo_de(e)).name
        zonas = zonas_de(e)
        if not zonas:
            uso[nombre] = u
            continue
        uso[nombre] = "vistas"
        vistas = _vistas_de(d, nombre) if d else []
        for z in zonas:
            k = _k_de_caja(vistas, z)
            if k is not None:
                uso[f"{nombre}#{k}"] = u
    return uso


CATEGORIAS_CAPA = [("muros", "Muros y tabiques"), ("puertas", "Puertas"), ("ventanas", "Ventanas"),
                   ("columnas", "Columnas / pilares"), ("ejes", "Ejes"), ("marcos", "Marcos de acero"),
                   ("losas", "Contorno de losas"), ("recintos", "Nombres de recintos")]


def _capas_en_planos(d: Path) -> tuple[list[str], list[dict]]:
    """Capas de las plantas y, por geometría, las que parecen tener muros (líneas paralelas)."""
    from cad2revit.muros import largo_pareado_por_capa
    py = _leer_yaml_seguro(d / "entradas" / "proyecto.yaml")
    archivos = [Path(p["archivo"]).name for p in py.get("plantas") or []] or [f.name for f in (d / "planos").glob("*.dxf")]
    det = (_leer_yaml_seguro(_lineamientos() / "capas_cad.yaml").get("deteccion_muros")
           or {"espesor_min": 0.05, "espesor_max": 0.40, "largo_min": 0.20})
    capas, pareado = set(), {}
    for nombre in archivos:
        f = d / "planos" / nombre
        if f.exists():
            try:
                dib = leer(str(f))
                capas |= {s.capa for s in dib.segmentos} | {i.capa for i in dib.inserciones} | {t.capa for t in dib.textos}
                for capa, (p, t) in largo_pareado_por_capa(dib.segmentos, det).items():
                    q = pareado.setdefault(capa, [0.0, 0.0])
                    q[0] += p
                    q[1] += t
            except Exception:
                pass
    from cad2revit.muros import NO_MURO
    cand = [(c, p / 2) for c, (p, t) in pareado.items() if p / 2 >= 5 and p >= 0.25 * t and not NO_MURO.search(c)]
    mejor = max((m for _, m in cand), default=0)
    sug = [{"capa": c, "metros": round(m, 1)} for c, m in cand if m >= 0.15 * mejor]
    sug.sort(key=lambda x: -x["metros"])
    return sorted(capas), sug[:6]


@app.get("/api/proyectos/{pid}/capas")
def leer_capas(pid: str):
    """Capas de las plantas del proyecto y, por categoría, cuáles se usan (elegidas o por la regla general)."""
    d = _proyecto(pid)
    capas, sug_muros = _capas_en_planos(d)
    propias = _leer_yaml_seguro(d / "entradas" / "proyecto.yaml").get("capas") or {}
    reglas = _leer_yaml_seguro(_lineamientos() / "capas_cad.yaml").get("capas") or {}
    out = []
    for clave, nombre in CATEGORIAS_CAPA:
        elegidas = propias.get(clave)
        if elegidas:
            usadas = [c for c in capas if c in elegidas]
        else:
            patron = reglas.get(clave)
            usadas = [c for c in capas if patron and re.search(patron, c)]
        out.append({"clave": clave, "nombre": nombre, "regla": reglas.get(clave), "elegidas": elegidas or None,
                    "usadas": usadas, "sugeridas": sug_muros if clave == "muros" else []})
    return {"capas_plano": capas, "categorias": out}


class CapasCategoria(BaseModel):
    categoria: str
    capas: list[str] | None = None        # None o [] = volver a la regla general


@app.put("/api/proyectos/{pid}/capas")
def guardar_capas(pid: str, datos: CapasCategoria):
    d = _proyecto(pid)
    if datos.categoria not in dict(CATEGORIAS_CAPA):
        raise HTTPException(400, f"Categoría desconocida: {datos.categoria}")
    py0 = _leer_yaml_seguro(d / "entradas" / "proyecto.yaml")
    if datos.categoria != "ejes" and any(py0.get(k) for k in ("tramos", "ejes_usuario", "muros_usuario", "muros_quitar", "marcos_usuario")):
        _fijar_ejes(d)          # cambiar capas no debe mover ni renombrar los ejes sobre los que ya se editó
    if datos.categoria not in dict(CATEGORIAS_CAPA):
        raise HTTPException(400, f"Categoría desconocida: {datos.categoria}")
    py = d / "entradas" / "proyecto.yaml"
    propias = dict(_leer_yaml_seguro(py).get("capas") or {})
    if datos.capas:
        propias[datos.categoria] = sorted(set(datos.capas))
    else:
        propias.pop(datos.categoria, None)
    texto = py.read_text(encoding="utf-8")
    if propias:
        texto = _reemplazar_bloque(texto, "capas", propias)
    else:
        texto = re.sub(r"^capas:.*?(?=^[^\s-]|\Z)", "", texto, flags=re.M | re.S)
    _escribir(py, texto)
    return {"guardado": True, "errores": _validar_entradas(d)}


class Nivel(BaseModel):
    nombre: str
    cota: float


class Distribucion(BaseModel):
    niveles: list[Nivel]
    usos: dict[str, str]          # archivo.dxf o archivo.dxf#k (vista k) -> "planta:N1" | "elevacion" | "no"


@app.get("/api/proyectos/{pid}/distribucion")
def leer_distribucion(pid: str):
    d = _proyecto(pid)
    py = _leer_yaml_seguro(d / "entradas" / "proyecto.yaml")
    return {"niveles": py.get("niveles") or [], "usos": _uso_de_planos(py, d)}


def _plantas_y_elevaciones(d: Path, usos: dict[str, str], nombres: list[str]) -> tuple[list, list, dict | None]:
    """De los usos (por plano o por vista) a los bloques plantas/elevaciones de proyecto.yaml."""
    existentes = {f.name for f in (d / "planos").glob("*.dxf")}
    con_vistas = {k.split("#")[0] for k in usos if "#" in k}
    plantas, elevaciones, ocupados, techo = {}, [], {}, None
    for clave, uso in sorted(usos.items()):
        archivo, _, k = clave.partition("#")
        if archivo not in existentes or uso in ("", "no", "vistas") or (not k and archivo in con_vistas):
            continue
        caja = None
        if k:
            vistas = _vistas_de(d, archivo)
            v = next((x for x in vistas if str(x["k"]) == k), None)
            if v is None:
                continue
            caja = v["caja"]
        if uso == "techo":
            if techo:
                raise HTTPException(400, "Solo puede haber una planta de techo")
            techo = {"archivo": f"../planos/{archivo}", "zona": caja} if caja else {"archivo": f"../planos/{archivo}"}
        elif uso == "elevacion":
            elevaciones.append({"archivo": f"../planos/{archivo}", "zona": caja} if caja else f"../planos/{archivo}")
        elif uso.startswith("planta:") and uso[7:] in nombres:
            nivel = uso[7:]
            if ocupados.get(nivel, archivo) != archivo:
                raise HTTPException(400, f"El nivel {nivel} tiene dos plantas ({ocupados[nivel]} y {archivo}); elige una")
            ocupados[nivel] = archivo
            p = plantas.setdefault(nivel, {"nivel": nivel, "archivo": f"../planos/{archivo}"})
            if caja:
                p.setdefault("zonas", []).append(caja)
            elif "zonas" in p:
                raise HTTPException(400, f"{archivo}: elige vistas sueltas o el plano completo, no ambos")
        else:
            raise HTTPException(400, f"Uso no válido para {clave}: {uso}")
    return list(plantas.values()), elevaciones, techo


@app.put("/api/proyectos/{pid}/distribucion")
def guardar_distribucion(pid: str, datos: Distribucion):
    """Niveles del proyecto y qué plano es la planta de cada nivel o una elevación."""
    d = _proyecto(pid)
    nombres = [n.nombre.strip() for n in datos.niveles]
    if not nombres or any(not n for n in nombres) or len(set(nombres)) != len(nombres):
        raise HTTPException(400, "Los niveles necesitan nombres distintos y no vacíos (al menos uno)")
    plantas, elevaciones, techo = _plantas_y_elevaciones(d, datos.usos, nombres)
    revisados = sorted({k.split("#")[0] for k in datos.usos if "#" in k})
    plantas.sort(key=lambda p: nombres.index(p["nivel"]))
    niveles = sorted(({"nombre": n.nombre.strip(), "cota": round(n.cota, 3)} for n in datos.niveles), key=lambda n: n["cota"])
    py = d / "entradas" / "proyecto.yaml"
    texto = py.read_text(encoding="utf-8")
    texto = _reemplazar_bloque(texto, "niveles", niveles)
    texto = _reemplazar_bloque(texto, "plantas", plantas)
    texto = _reemplazar_bloque(texto, "elevaciones", elevaciones)
    texto = _reemplazar_bloque(texto, "techo", techo)
    if revisados:     # el usuario eligió las vistas de estos planos: la app ya no las cambia sola
        previos = set(_leer_yaml_seguro(py).get("vistas_revisadas") or []) if py.exists() else set()
        texto = _reemplazar_bloque(texto, "vistas_revisadas", sorted(previos | set(revisados)))
    _escribir(py, texto)
    return {"guardado": True, "errores": _validar_entradas(d)}


# ---------------------------------------------------------------- planos
@app.get("/api/proyectos/{pid}/planos")
def listar_planos(pid: str):
    d = _proyecto(pid)
    _separar_vistas(d)
    uso = _uso_de_planos(_leer_yaml_seguro(d / "entradas" / "proyecto.yaml"), d)
    patron_nivel = _leer_yaml_seguro(_lineamientos() / "capas_cad.yaml").get("patron_nivel")
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
        original = d / "planos" / "originales" / (f.stem + ".dwg")
        out.append({"nombre": f.name, "bytes": f.stat().st_size, "capas": capas, "bloques": bloques,
                    "ruta_en_proyecto": f"../planos/{f.name}", "error": err,
                    "desde_dwg": original.name if original.exists() else None,
                    "uso": uso.get(f.name, "no"),
                    "ejemplo": f.name in PLANOS_EJEMPLO,
                    "vistas": _vistas_con_uso(d, f.name, uso) if not err else [],
                    "parece_elevacion": not err and _parece_elevacion(dib, patron_nivel)})
    return out


@app.post("/api/proyectos/{pid}/planos", status_code=201)
async def subir_planos(pid: str, archivos: list[UploadFile] = File(...)):
    d = _proyecto(pid)
    guardados = []
    for up in archivos:
        nombre = _seguro(Path(up.filename or "").name, "nombre de archivo")
        ext = Path(nombre).suffix.lower()
        if ext not in (".dxf", ".dwg"):
            raise HTTPException(400, f"{nombre}: solo se aceptan planos .dwg o .dxf")
        contenido = await up.read()
        if len(contenido) > MAX_SUBIDA:
            raise HTTPException(413, f"{nombre}: supera {MAX_SUBIDA // 1024 // 1024} MB")
        if ext == ".dwg":
            # Formato de trabajo: DXF. El DWG original se guarda tal cual en planos/originales/.
            originales = d / "planos" / "originales"
            originales.mkdir(exist_ok=True)
            origen = originales / (Path(nombre).stem + ".dwg")
            origen.write_bytes(contenido)
            destino = d / "planos" / (Path(nombre).stem + ".dxf")
            try:
                _convertir_dwg(origen, destino)
                leer(str(destino))             # valida y deja listo el formato interno
            except HTTPException:
                origen.unlink(missing_ok=True)
                raise
            except Exception as e:
                origen.unlink(missing_ok=True)
                destino.unlink(missing_ok=True)
                raise HTTPException(400, f"{nombre}: el DXF convertido no se pudo leer ({e})")
            guardados.append(destino.name)
            continue
        (d / "planos" / nombre).write_bytes(contenido)
        guardados.append(nombre)
    return {"guardados": guardados, **_acomodar_planos(d, guardados)}


def _separar_vistas(d: Path) -> list[str]:
    """Mantiene coherente el uso de las vistas de cada plano (un plano = UN edificio):
    - plano usado completo y con varias vistas -> solo la planta sugerida genera muros; cortes, fachadas y
      planta de techo dan alturas y cubierta;
    - vistas guardadas que ya no existen (la detección mejoró y las vistas cambiaron) -> se reasignan,
      aunque se hubieran elegido a mano, porque ya no corresponden a ningún dibujo;
    - planta ya separada sin cortes/techo -> se completan (salvo que el usuario haya elegido las vistas)."""
    from cad2revit.vistas import archivo_de, zonas_de
    py = d / "entradas" / "proyecto.yaml"
    datos = _leer_yaml_seguro(py)
    notas, cambio = [], False
    elevaciones = list(datos.get("elevaciones") or [])
    plantas = list(datos.get("plantas") or [])
    revisados = set(datos.get("vistas_revisadas") or [])
    techo = datos.get("techo")
    nombre_de = lambda e: Path(archivo_de(e)).name  # noqa: E731
    for pl in plantas:
        nombre = Path(pl["archivo"]).name
        try:
            vistas = _vistas_de(d, nombre)
        except Exception:
            continue
        if len(vistas) < 2:
            continue
        del_archivo = [pl] + [e for e in elevaciones if nombre_de(e) == nombre] + \
                      ([techo] if techo and nombre_de(techo) == nombre else [])
        obsoletas = any(_k_de_caja(vistas, z) is None for e in del_archivo for z in zonas_de(e))
        completo = not pl.get("zonas")
        if obsoletas or (completo and nombre not in revisados):
            # reasignar todas las vistas de este plano según la sugerencia
            elevaciones = [e for e in elevaciones if nombre_de(e) != nombre]
            if techo and nombre_de(techo) == nombre:
                techo = None
            planta = next(v for v in vistas if v["sugerido"] == "planta")
            pl["zonas"] = [planta["caja"]]
            for v in vistas:
                if v["sugerido"] == "elevacion":
                    elevaciones.append({"archivo": pl["archivo"], "zona": v["caja"]})
                elif v["sugerido"] == "techo" and not techo:
                    techo = {"archivo": pl["archivo"], "zona": v["caja"]}
            revisados.discard(nombre)
            motivo = ("las vistas guardadas eran de una versión anterior y ya no coinciden con el dibujo"
                      if obsoletas else f"trae {len(vistas)} vistas")
            notas.append(f"{nombre}: {motivo}; se usa la vista {planta['k'] + 1} como planta de {pl['nivel']} y las "
                         "demás (cortes, fachadas, planta de techo) dan las alturas y la forma de la cubierta, sin "
                         "generar estructura. Puedes cambiarlo en Planos.")
            cambio = True
            continue
        ya = {nombre_de(e) for e in elevaciones} | ({nombre_de(techo)} if techo else set())
        if nombre in revisados or nombre in ya:
            continue
        nuevas = [v for v in vistas if v["sugerido"] in ("elevacion", "techo")]
        for v in nuevas:
            if v["sugerido"] == "techo" and not techo:
                techo = {"archivo": pl["archivo"], "zona": v["caja"]}
            elif v["sugerido"] == "elevacion":
                elevaciones.append({"archivo": pl["archivo"], "zona": v["caja"]})
        if nuevas:
            notas.append(f"{nombre}: se agregaron {len(nuevas)} vistas (cortes, fachadas, planta de techo) "
                         "para las alturas y la forma de la cubierta. Puedes cambiarlo en Planos.")
            cambio = True
    if cambio:
        texto = py.read_text(encoding="utf-8")
        texto = _reemplazar_bloque(texto, "plantas", plantas)
        texto = _reemplazar_bloque(texto, "elevaciones", elevaciones)
        texto = _reemplazar_bloque(texto, "techo", techo)
        texto = _reemplazar_bloque(texto, "vistas_revisadas", sorted(revisados))
        _escribir(py, texto)
    return notas


def _vistas_con_uso(d: Path, nombre: str, uso: dict) -> list[dict]:
    """Vistas de un plano con su uso actual (solo si el plano tiene más de una)."""
    vistas = _vistas_de(d, nombre)
    if len(vistas) < 2:
        return []
    entero = uso.get(nombre, "no")
    return [dict(v, uso=uso.get(f"{nombre}#{v['k']}", "no" if entero == "vistas" else entero)) for v in vistas]


PLANOS_EJEMPLO = {"planta_n1.dxf", "planta_n2.dxf", "elevacion_norte.dxf"}


def _acomodar_planos(d: Path, nuevos: list[str]) -> dict:
    from cad2revit.vistas import archivo_de
    """Al subir planos propios: se quitan los de ejemplo y cada plano nuevo queda asignado solo
    (elevación si tiene textos de nivel; si no, planta del primer nivel libre). Se cambia en la tabla."""
    quitados = []
    if any(n not in PLANOS_EJEMPLO for n in nuevos):
        for n in sorted(PLANOS_EJEMPLO - set(nuevos)):
            if (d / "planos" / n).exists():
                _borrar_archivo_plano(d / "planos" / n)
                quitados.append(n)
    py = d / "entradas" / "proyecto.yaml"
    datos = _leer_yaml_seguro(py)
    existe = {f.name for f in (d / "planos").glob("*.dxf")}
    plantas = [x for x in datos.get("plantas") or [] if Path(x["archivo"]).name in existe]
    elevaciones = [x for x in datos.get("elevaciones") or [] if Path(archivo_de(x)).name in existe]
    niveles = sorted(datos.get("niveles") or [], key=lambda n: n.get("cota", 0))
    candidatos = [n["nombre"] for n in (niveles[:-1] if len(niveles) > 1 else niveles)]
    patron = _leer_yaml_seguro(_lineamientos() / "capas_cad.yaml").get("patron_nivel")
    techo = datos.get("techo")
    if techo and Path(archivo_de(techo)).name not in existe:
        techo = None
    usados = {Path(x["archivo"]).name for x in plantas} | {Path(archivo_de(x)).name for x in elevaciones}
    asignados = {}
    for n in sorted(set(nuevos) & existe - usados):
        try:
            dib = leer(str(d / "planos" / n))
        except Exception:
            continue
        libres = [c for c in candidatos if c not in {x["nivel"] for x in plantas}]
        vistas = _vistas_de(d, n)
        if len(vistas) > 1:
            # varias vistas en el mismo plano: la planta sugerida va al primer nivel libre, el resto según sugerencia
            zonas = [v["caja"] for v in vistas if v["sugerido"] == "planta"]
            if zonas and libres:
                plantas.append({"nivel": libres[0], "archivo": f"../planos/{n}", "zonas": zonas})
                asignados[n] = f"planta:{libres[0]} ({len(zonas)} vista{'s' if len(zonas) > 1 else ''})"
            for v in vistas:
                if v["sugerido"] == "elevacion":
                    elevaciones.append({"archivo": f"../planos/{n}", "zona": v["caja"]})
                elif v["sugerido"] == "techo" and not techo:
                    techo = {"archivo": f"../planos/{n}", "zona": v["caja"]}
            continue
        if _parece_elevacion(dib, patron):
            elevaciones.append(f"../planos/{n}")
            asignados[n] = "elevacion"
            continue
        if libres:
            plantas.append({"nivel": libres[0], "archivo": f"../planos/{n}"})
            asignados[n] = f"planta:{libres[0]}"
    orden = [n["nombre"] for n in niveles]
    plantas.sort(key=lambda x: orden.index(x["nivel"]) if x["nivel"] in orden else 99)
    texto = py.read_text(encoding="utf-8")
    texto = _reemplazar_bloque(texto, "plantas", plantas)
    texto = _reemplazar_bloque(texto, "elevaciones", elevaciones)
    texto = _reemplazar_bloque(texto, "techo", techo)
    _escribir(py, texto)
    return {"asignados": asignados, "ejemplos_quitados": quitados}


def _parece_elevacion(dib, patron) -> bool:
    return bool(patron and any(re.search(patron, t.texto) for t in dib.textos))


def _borrar_archivo_plano(f: Path) -> None:
    f.unlink()
    (f.parent / "originales" / (f.stem + ".dwg")).unlink(missing_ok=True)
    for c in (f.parent / ".cad2revit").glob(f"{f.name}.*"):        # formato interno del plano
        c.unlink(missing_ok=True)


@app.delete("/api/proyectos/{pid}/planos/{nombre}", status_code=204)
def borrar_plano(pid: str, nombre: str):
    f = _proyecto(pid) / "planos" / _seguro(nombre)
    if not f.exists():
        raise HTTPException(404, "No existe el plano")
    _borrar_archivo_plano(f)


@app.get("/api/proyectos/{pid}/planos/{nombre}/archivo")
def descargar_plano(pid: str, nombre: str):
    """El plano en el formato de trabajo (DXF; si se subió un DWG, el DXF convertido)."""
    f = _proyecto(pid) / "planos" / _seguro(nombre)
    if not f.is_file():
        raise HTTPException(404, "No existe el plano")
    return FileResponse(f, filename=f.name, media_type="application/dxf")


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
        separadas = _separar_vistas(d)
        modelo = construir_modelo(cargar(d / "entradas" / "proyecto.yaml"))
        modelo["advertencias"][:0] = separadas
        usados = _uso_de_planos(_leer_yaml_seguro(d / "entradas" / "proyecto.yaml"))       # sin d: solo nombres
        sin_usar = sorted(f.name for f in (d / "planos").glob("*.dxf") if f.name not in usados)
        if sin_usar:
            modelo["advertencias"].insert(0, "Planos subidos que NO se usan en esta corrida (asígnalos a un nivel en la "
                                             "pestaña Planos): " + ", ".join(sin_usar))
        resumen["planos_sin_usar"] = sin_usar
        resumen["errores_esquema"] = validar(modelo)
        resumen["ok"] = not resumen["errores_esquema"]
        resumen["conteos"] = _conteos(modelo)
        resumen["lod"] = modelo["lod"]["preset"]
        _escribir((c / "modelo.json"), json.dumps(modelo, ensure_ascii=False, indent=1))
    except ErrorConfiguracion as e:
        resumen["error"] = str(e)
    except Exception as e:
        resumen["error"] = f"Error inesperado al procesar: {type(e).__name__}: {e}"
        resumen["traza"] = traceback.format_exc(limit=5)
    _escribir((c / "resumen.json"), json.dumps(resumen, ensure_ascii=False, indent=2))
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


def _orden_natural(nombre: str):
    return [(0, int(t), "") if t.isdigit() else (1, 0, t.upper()) for t in re.findall(r"\d+|\D+", str(nombre))]


def ejes_y_niveles(m: dict, n: int | None = None, vistas_cortes: list | None = None) -> dict:
    """Modelo básico, ordenado: niveles de abajo hacia arriba (con la altura de cada piso) y ejes agrupados en
    verticales (de izquierda a derecha) y horizontales (de abajo hacia arriba), con su posición y la distancia
    al anterior; cortes y fachadas como referencia. Coordenadas al milímetro. El complemento de Revit solo crea
    o ajusta niveles y ejes con este archivo (alcance), sin tocar nada más."""
    r3 = lambda v: round(float(v), 3)                                      # noqa: E731
    niveles = sorted(m.get("niveles") or [], key=lambda x: x["cota"])
    niv = []
    for i, x in enumerate(niveles):
        d = {"nombre": x["nombre"], "cota": r3(x["cota"])}
        if i + 1 < len(niveles):
            d["altura_hasta_siguiente"] = r3(niveles[i + 1]["cota"] - x["cota"])
        niv.append(d)
    verticales, horizontales, otros = [], [], []
    for e in m.get("ejes") or []:
        (x1, y1), (x2, y2) = e["inicio"], e["fin"]
        if abs(x2 - x1) <= 0.01 * abs(y2 - y1):
            a, b = sorted([(r3(x1), r3(y1)), (r3(x2), r3(y2))], key=lambda q: q[1])     # de abajo hacia arriba
            verticales.append({"nombre": e["nombre"], "inicio": list(a), "fin": list(b), "orientacion": "vertical",
                               "x": r3((x1 + x2) / 2)})
        elif abs(y2 - y1) <= 0.01 * abs(x2 - x1):
            a, b = sorted([(r3(x1), r3(y1)), (r3(x2), r3(y2))], key=lambda q: q[0])     # de izquierda a derecha
            horizontales.append({"nombre": e["nombre"], "inicio": list(a), "fin": list(b), "orientacion": "horizontal",
                                 "y": r3((y1 + y2) / 2)})
        else:
            otros.append({"nombre": e["nombre"], "inicio": [r3(x1), r3(y1)], "fin": [r3(x2), r3(y2)],
                          "orientacion": "inclinado"})
    verticales.sort(key=lambda e: (e["x"], _orden_natural(e["nombre"])))
    horizontales.sort(key=lambda e: (e["y"], _orden_natural(e["nombre"])))
    otros.sort(key=lambda e: _orden_natural(e["nombre"]))
    for grupo, k in ((verticales, "x"), (horizontales, "y")):
        for i, e in enumerate(grupo):
            if i:
                e["distancia_anterior"] = r3(e[k] - grupo[i - 1][k])
    cortes = []
    lista = m.get("cortes") or []
    nums = vistas_cortes if vistas_cortes and len(vistas_cortes) == len(lista) else [None] * len(lista)
    for c, vn in zip(lista, nums):
        es_corte = c.get("tipo") == "corte"
        titulo = c.get("titulo") if es_corte else ("Fachada" + (f" (vista {vn})" if vn else ""))
        d = {"titulo": titulo, "tipo": "corte" if es_corte else "fachada", "letra": c.get("letra")}
        if vn:
            d["vista"] = vn
        d["cumbrera"] = c.get("cumbrera")
        if c.get("pendiente"):
            d["alero"], d["pendiente"] = c.get("alero"), c.get("pendiente")
        if c.get("linea"):
            (x1, y1), (x2, y2) = c["linea"]
            # línea de corte ortogonal si lo es casi (marcas dibujadas unos cm corridas)
            if abs(x2 - x1) < 0.06 * abs(y2 - y1):
                x1 = x2 = (x1 + x2) / 2
            elif abs(y2 - y1) < 0.06 * abs(x2 - x1):
                y1 = y2 = (y1 + y2) / 2
            d["linea"] = [[r3(x1), r3(y1)], [r3(x2), r3(y2)]]
        cortes.append(d)
    cortes.sort(key=lambda c: (c["tipo"] != "corte", _orden_natural(c.get("letra") or ""), c.get("vista") or 0))
    fund = m.get("fundaciones")
    return {
        "version": m.get("version"), "unidades": m.get("unidades", "m"), "proyecto": m.get("proyecto", ""),
        "alcance": ["niveles", "ejes"],
        "lod": m.get("lod") or {},
        "niveles": niv,
        "ejes": verticales + horizontales + otros,
        "resumen_ejes": {
            "verticales": [f"{e['nombre']} x={e['x']}" + (f" (+{e['distancia_anterior']})" if "distancia_anterior" in e else "")
                           for e in verticales],
            "horizontales": [f"{e['nombre']} y={e['y']}" + (f" (+{e['distancia_anterior']})" if "distancia_anterior" in e else "")
                             for e in horizontales],
        },
        "cortes": cortes,
        "materiales": [], "tipos_muro": [], "tipos_losa": [], "muros": [], "aberturas": [], "losas": [],
        "columnas": [], "vigas": [], "fundaciones": {k: [] for k in fund} if isinstance(fund, dict) else [],
        "recintos": [],
        "advertencias": [f"Modelo básico{f' de la corrida {n}' if n else ''}: {len(niv)} niveles y "
                         f"{len(verticales) + len(horizontales) + len(otros)} ejes ({len(verticales)} verticales, "
                         f"{len(horizontales)} horizontales)"],
    }


def aberturas_ordenadas(m: dict, n: int | None = None) -> dict:
    """Puertas y ventanas de la corrida, para abrir sus vanos en un modelo de Revit (botón «Puertas y ventanas»):
    nivel, eje del muro, posición (centro en el eje del muro), ancho, alto y antepecho; ordenadas por nivel, eje y
    posición a lo largo del eje. Incluye el origen que usa la importación, por si el archivo de Revit no lo tiene."""
    r3 = lambda v: round(float(v), 3)                                      # noqa: E731
    cota = {x["nombre"]: x["cota"] for x in m.get("niveles") or []}
    muros = {w["id"]: w for w in m.get("muros") or []}
    org = ejes_y_niveles(m)["ejes"]
    orden_eje = {e["nombre"]: i for i, e in enumerate(org)}

    def eje_de(w):
        (x1, y1), (x2, y2) = w["inicio"], w["fin"]
        L = math.hypot(x2 - x1, y2 - y1) or 1.0
        for e in m.get("ejes") or []:
            (a1, b1), (a2, b2) = e["inicio"], e["fin"]
            le = math.hypot(a2 - a1, b2 - b1) or 1.0
            if abs((x2 - x1) / L * (b2 - b1) / le - (y2 - y1) / L * (a2 - a1) / le) > 0.02:
                continue
            if all(abs((q[0] - a1) * (b2 - b1) - (q[1] - b1) * (a2 - a1)) / le < 0.02 for q in (w["inicio"], w["fin"])):
                return e["nombre"]
        return None

    out = []
    # las que quedaron sin muro en la app (se eliminó en Estructuración) también van: en Revit se abren en el muro
    # que haya en ese lugar; su recta es la del muro original
    lista = [(a, muros.get(a["muro"])) for a in m.get("aberturas") or []]
    for a in m.get("aberturas_sin_muro") or []:
        if a.get("linea") and len(a["linea"]) == 2:
            lista.append((a, {"id": None, "nivel": a.get("nivel"), "tipo": None,
                              "inicio": a["linea"][0], "fin": a["linea"][1]}))
    for a, w in lista:
        if not w:
            continue
        (x1, y1), (x2, y2) = w["inicio"], w["fin"]
        L = math.hypot(x2 - x1, y2 - y1) or 1.0
        u = ((x2 - x1) / L, (y2 - y1) / L)
        if (u[0] < -1e-9) or (abs(u[0]) < 1e-9 and u[1] < 0):
            u = (-u[0], -u[1])                       # dirección de izquierda a derecha / de abajo hacia arriba
        eje = eje_de(w)
        # posición desde los ejes perpendiculares vecinos (a cada lado): Revit la ubica desde sus propios ejes
        horizontal_m = abs(u[0]) >= abs(u[1])
        tc = a["centro"][0] if horizontal_m else a["centro"][1]
        perp = []
        for e in m.get("ejes") or []:
            (ex1, ey1), (ex2, ey2) = e["inicio"], e["fin"]
            vertical_e = abs(ex2 - ex1) < abs(ey2 - ey1)
            if vertical_e == horizontal_m:
                perp.append(((ex1 + ex2) / 2 if vertical_e else (ey1 + ey2) / 2, e["nombre"]))
        antes = max((q for q in perp if q[0] <= tc + 1e-6), default=None)
        despues = min((q for q in perp if q[0] >= tc - 1e-6), default=None)
        if antes and despues and antes[1] == despues[1]:          # justo sobre un eje: basta una referencia
            despues = None
        refs = [{"eje": q[1], "desde": r3(tc - q[0])} for q in (antes, despues) if q]
        out.append({
            "id": a["id"], "categoria": a["categoria"], "nivel": w["nivel"], "cota_nivel": r3(cota.get(w["nivel"], 0.0)),
            "eje": eje, "muro": w["id"], "tipo_muro": w.get("tipo"), "direccion_muro": [round(u[0], 6), round(u[1], 6)],
            "centro": [r3(a["centro"][0]), r3(a["centro"][1])],
            "ancho": r3(a["ancho"]), "alto": r3(a["alto"]), "antepecho": r3(a["antepecho"]),
            "referencias": refs,
        })
    out.sort(key=lambda a: (cota.get(a["nivel"], 0.0), orden_eje.get(a["eje"], 10 ** 6), a["eje"] or "",
                            a["centro"][0] * a["direccion_muro"][0] + a["centro"][1] * a["direccion_muro"][1]))
    xs = [p[0] for w in m.get("muros") or [] for p in (w["inicio"], w["fin"])] + \
         [p[0] for e in m.get("ejes") or [] for p in (e["inicio"], e["fin"])]
    ys = [p[1] for w in m.get("muros") or [] for p in (w["inicio"], w["fin"])] + \
         [p[1] for e in m.get("ejes") or [] for p in (e["inicio"], e["fin"])]
    origen = [0.0, 0.0]
    if xs and not (abs(min(xs)) < 200 and abs(min(ys)) < 200):
        origen = [math.floor(min(xs) / 10) * 10, math.floor(min(ys) / 10) * 10]
    return {
        "tipo_archivo": "cad2revit-aberturas", "version": m.get("version"), "unidades": "m",
        "proyecto": m.get("proyecto", ""), "corrida": n, "origen_sugerido": origen,
        "resumen": {"puertas": sum(a["categoria"] == "puerta" for a in out),
                    "ventanas": sum(a["categoria"] == "ventana" for a in out)},
        "aberturas": out,
    }


@app.get("/api/proyectos/{pid}/corridas/{n}/aberturas.json")
def descargar_aberturas(pid: str, n: int):
    """Puertas y ventanas ordenadas de la corrida (ver aberturas_ordenadas)."""
    d = _proyecto(pid)
    f = _corrida_dir(d, n) / "modelo.json"
    if not f.exists():
        raise HTTPException(404, "Esta corrida no generó modelo (tuvo errores)")
    datos = aberturas_ordenadas(json.loads(f.read_text(encoding="utf-8")), n)
    return Response(json.dumps(datos, ensure_ascii=False, indent=1), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{pid}-corrida{n}-aberturas.json"'})


@app.get("/api/proyectos/{pid}/corridas/{n}/ejes-niveles.json")
def descargar_ejes_niveles(pid: str, n: int):
    """Modelo básico de la corrida: solo niveles y ejes, ordenados (ver ejes_y_niveles)."""
    d = _proyecto(pid)
    f = _corrida_dir(d, n) / "modelo.json"
    if not f.exists():
        raise HTTPException(404, "Esta corrida no generó modelo (tuvo errores)")
    # número de vista (como en Planos) de cada corte o fachada, en el orden de las elevaciones del proyecto
    nums = []
    try:
        from cad2revit.vistas import archivo_de, zonas_de
        py = _leer_yaml_seguro(d / "entradas" / "proyecto.yaml")
        for e in py.get("elevaciones") or []:
            zonas = zonas_de(e)
            k = _k_de_caja(_vistas_de(d, Path(archivo_de(e)).name), zonas[0]) if zonas else None
            nums.append(k + 1 if k is not None else None)
    except Exception:
        nums = []
    basico = ejes_y_niveles(json.loads(f.read_text(encoding="utf-8")), n, nums)
    return Response(json.dumps(basico, ensure_ascii=False, indent=1), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{pid}-corrida{n}-ejes-niveles.json"'})


@app.put("/api/proyectos/{pid}/corridas/{n}/notas")
def guardar_notas(pid: str, n: int, datos: Notas):
    c = _corrida_dir(_proyecto(pid), n)
    r = json.loads((c / "resumen.json").read_text(encoding="utf-8"))
    r["notas"] = datos.notas
    _escribir((c / "resumen.json"), json.dumps(r, ensure_ascii=False, indent=2))
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


# ------------------------------------------------------------------ Análisis y diseño de ejes exportados desde Revit
# (PyNite en la nube; el .s2k y el agente local de SAP2000 quedan como borrador)

class AjustesEje(BaseModel):
    parametros: dict = {}
    opciones: dict = {}


def _eje_sap(d: Path, nombre: str) -> Path:
    f = d / "sap" / f"{nombre}.sap.json"
    if not re.fullmatch(r"[\w.\-]+", nombre) or not f.is_file():
        raise HTTPException(404, f"No existe el eje {nombre}")
    return f


def _ajustes_eje(f: Path) -> dict:
    a = f.with_name(f.name.replace(".sap.json", ".ajustes.json"))
    return json.loads(a.read_text(encoding="utf-8")) if a.is_file() else {"parametros": {}, "opciones": {}}


@app.get("/api/proyectos/{pid}/sap")
def listar_ejes_sap(pid: str):
    d = _proyecto(pid)
    out = []
    for f in sorted((d / "sap").glob("*.sap.json")):
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({"nombre": f.name[: -len(".sap.json")], "eje": e.get("eje"), "proyecto_revit": e.get("proyecto"),
                    "fecha": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                    "pilares": len(e.get("pilares") or []), "vigas": len(e.get("vigas") or []), "muros": len(e.get("muros") or [])})
    return out


@app.post("/api/proyectos/{pid}/sap", status_code=201)
async def subir_eje_sap(pid: str, archivo: UploadFile = File(...)):
    """El .sap.json del botón «Eje a análisis» de Revit (lo sube el propio botón o se arrastra a la pestaña)."""
    d = _proyecto(pid)
    try:
        e = json.loads((await archivo.read()).decode("utf-8"))
    except Exception:
        raise HTTPException(400, "El archivo no es JSON")
    if e.get("tipo_archivo") != "cad2revit-eje-sap":
        raise HTTPException(400, "No es un archivo del botón «Eje a análisis» de Revit")
    nombre = "eje-" + re.sub(r"[^\w.\-]", "_", str(e.get("eje") or "x"))
    (d / "sap").mkdir(exist_ok=True)
    (d / "sap" / f"{nombre}.sap.json").write_text(json.dumps(e, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"nombre": nombre}


@app.get("/api/proyectos/{pid}/sap/{nombre}")
def leer_eje_sap(pid: str, nombre: str):
    f = _eje_sap(_proyecto(pid), nombre)
    return {"eje": json.loads(f.read_text(encoding="utf-8")), "ajustes": _ajustes_eje(f)}


@app.delete("/api/proyectos/{pid}/sap/{nombre}", status_code=204)
def borrar_eje_sap(pid: str, nombre: str):
    f = _eje_sap(_proyecto(pid), nombre)
    f.unlink()
    for ext in (".ajustes.json", ".resultado.json", ".fundacion.json", ".detalle.png", ".detalle.json"):
        a = f.with_name(f.name.replace(".sap.json", ext))
        if a.is_file():
            a.unlink()


@app.put("/api/proyectos/{pid}/sap/{nombre}/ajustes")
def guardar_ajustes_eje(pid: str, nombre: str, datos: AjustesEje):
    f = _eje_sap(_proyecto(pid), nombre)
    f.with_name(f.name.replace(".sap.json", ".ajustes.json")).write_text(
        json.dumps({"parametros": datos.parametros, "opciones": datos.opciones}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"guardado": True}


def _modelo_eje(eje: dict, aj: AjustesEje):
    from app.sap.eje_cargas import ParametrosCargas, cargas_eje
    from app.sap.eje_revit import ParametrosMuroMadera, mallar
    par = aj.parametros or {}
    try:
        pm = ParametrosMuroMadera(**{k: v for k, v in (par.get("muro") or {}).items()
                                     if k in ParametrosMuroMadera.__dataclass_fields__ and v not in (None, "")})
        mod = mallar(eje, pm, malla=float(par.get("malla") or 0.5), apoyo_pilares=par.get("apoyo_pilares") or "rotula",
                     opciones=aj.opciones)
        pc = ParametrosCargas(**{k: v for k, v in (par.get("cargas") or {}).items()
                                 if k in ParametrosCargas.__dataclass_fields__ and v not in (None, "")})
        return mod, pc, cargas_eje(mod, pc)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, f"Parámetros no válidos: {e}")


def _ids_costaneras(d: Path) -> set[str]:
    """Ids de Revit de las costaneras (miembros de sistemas de vigas) de los techos del proyecto."""
    ids = set()
    for f in (d / "costaneras").glob("*.costaneras.json"):
        try:
            ids.update(str(c.get("id")) for c in json.loads(f.read_text(encoding="utf-8")).get("costaneras") or [])
        except Exception:
            continue
    return ids


@app.post("/api/proyectos/{pid}/sap/{nombre}/vista")
def vista_eje_sap(pid: str, nombre: str, aj: AjustesEje):
    d = _proyecto(pid)
    f = _eje_sap(d, nombre)
    eje = json.loads(f.read_text(encoding="utf-8"))
    mod, pc, cargas = _modelo_eje(eje, aj)
    cos = _ids_costaneras(d)
    excl = {str(x) for x in (aj.opciones or {}).get("excluir") or []}
    costaneras = [str(v.get("id")) for v in eje.get("vigas") or [] if str(v.get("id")) in cos or v.get("sistema_vigas")]
    return {
        "sin_apoyo": mod.sin_apoyo,
        "sugeridos": [{"id": i, "motivo": "costanera de techo (sistema de vigas)"} for i in costaneras if i not in excl]
                     + [{"id": i, "motivo": "sin apoyo en el plano del eje"} for i in mod.sin_apoyo if i not in costaneras],
        "nombre": mod.nombre, "nudos": mod.nudos, "apoyos": mod.apoyos, "apoyos_defecto": mod.apoyos_defecto,
        "liberaciones": {k: list(v) for k, v in mod.liberaciones.items()},
        "barras": [{"nombre": b, "ni": a, "nj": c, "seccion": sec, "rol": mod.roles.get(b), "origen": mod.origen.get(b)}
                   for b, a, c, sec in mod.barras],
        "areas": [{"nombre": a, "nudos": ns, "seccion": sec, "origen": mod.origen.get(a)} for a, ns, sec in mod.areas],
        "secciones": mod.secciones, "secciones_area": mod.secciones_area, "muros": mod.resumen_muros,
        "cargas": cargas.resumen, "combos": list(cargas.combos), "avisos": mod.avisos,
        "ancho_tributario": mod.ancho_tributario,
        "conteo": {"nudos": len(mod.nudos), "barras": len(mod.barras), "shells": len(mod.areas)},
    }


@app.post("/api/proyectos/{pid}/sap/{nombre}/s2k")
def s2k_eje_sap(pid: str, nombre: str, aj: AjustesEje):
    import tempfile
    from app.sap.eje_revit import escribir_s2k
    f = _eje_sap(_proyecto(pid), nombre)
    mod, _, cargas = _modelo_eje(json.loads(f.read_text(encoding="utf-8")), aj)
    with tempfile.TemporaryDirectory() as tmp:
        ruta = Path(tmp) / f"{nombre}.s2k"
        escribir_s2k(mod, str(ruta), cargas)
        return Response(ruta.read_text(encoding="utf-8"), media_type="text/plain",
                        headers={"Content-Disposition": f'attachment; filename="{nombre}.s2k"'})


_AVANCE_ANALISIS: dict = {}       # "pid/eje" → {pct, etapa}: avance del análisis en curso


@app.get("/api/proyectos/{pid}/sap/{nombre}/avance")
def avance_eje_pynite(pid: str, nombre: str):
    return _AVANCE_ANALISIS.get(f"{pid}/{nombre}") or {"pct": None, "etapa": ""}


@app.post("/api/proyectos/{pid}/sap/{nombre}/analizar")
def analizar_eje_pynite(pid: str, nombre: str, aj: AjustesEje):
    """Análisis en la nube con PyNite (estático + modal espectral NCh433/DS61) y verificación de los elementos:
    acero AISC 360-16 LRFD y muros de madera (Manual, tensiones admisibles). Guarda el resultado junto al eje."""
    from app.sap.eje_pynite import analizar
    f = _eje_sap(_proyecto(pid), nombre)
    mod, pc, cargas = _modelo_eje(json.loads(f.read_text(encoding="utf-8")), aj)
    dis = (aj.parametros or {}).get("diseno") or {}
    try:
        fy = float(dis.get("fy_mpa") or 270) * 1000 / 9.80665
        lb = float(dis["lb"]) if dis.get("lb") not in (None, "") else None
        clave = f"{pid}/{nombre}"
        _AVANCE_ANALISIS[clave] = {"pct": 0, "etapa": "Preparando el modelo", "hasta": 2}
        limites = {k: float(dis[k]) for k in ("viga_total", "viga_variable", "lateral_viento", "deriva_sismo")
                   if dis.get(k) not in (None, "")}
        masa = "consistente" if dis.get("masa") == "consistente" else "concentrada"     # por defecto, como SAP2000
        ry = float(dis.get("ry") or 1.5)
        r = analizar(mod, cargas, pc, fy=fy, lb_fuera=lb, limites=limites, masa=masa, ry=ry, avance=lambda pct, etapa, hasta: _AVANCE_ANALISIS.__setitem__(
            clave, {"pct": pct, "etapa": etapa, "hasta": hasta}))
    except HTTPException:
        raise
    except Exception as e:
        txt = str(e)
        if "singular" in txt.lower() or "unstable" in txt.lower():
            txt = ("el modelo es inestable (la matriz de rigidez es singular): revisa los apoyos (alguno deslizante o libre "
                   "que deje el eje sin restricción horizontal o vertical) y las uniones rotuladas (una viga o pilar rotulado "
                   "en ambos extremos sin otro elemento que lo estabilice), y quita los elementos que no son parte del sistema "
                   "sismorresistente")
        raise HTTPException(400, f"No se pudo analizar el eje: {txt}")
    finally:
        _AVANCE_ANALISIS.pop(f"{pid}/{nombre}", None)
    r["fecha"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    r["nota"] = f"Análisis PyNite en la app (masa {masa}, Fy {dis.get('fy_mpa') or 270} MPa" + (f", Lb {lb} m" if lb else ", Lb = largo del elemento") + ")"
    # numpy (np.float64, np.bool_, arreglos) → tipos de JSON, para guardar y responder sin errores
    texto = json.dumps(r, ensure_ascii=False, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
    f.with_name(f.name.replace(".sap.json", ".resultado.json")).write_text(texto, encoding="utf-8")
    return Response(texto, media_type="application/json")


@app.get("/api/proyectos/{pid}/sap/{nombre}/resultado")
def resultado_eje_pynite(pid: str, nombre: str):
    f = _eje_sap(_proyecto(pid), nombre)
    r = f.with_name(f.name.replace(".sap.json", ".resultado.json"))
    if not r.is_file():
        raise HTTPException(404, "El eje aún no se ha analizado")
    from app.sap.eje_pynite import VERSION_RESULTADO
    d = json.loads(r.read_text(encoding="utf-8"))
    d["desactualizado"] = d.get("version") != VERSION_RESULTADO
    return d


def _detalle_eje(f: Path, ext: str) -> Path:
    return f.with_name(f.name.replace(".sap.json", ext))


@app.post("/api/proyectos/{pid}/sap/{nombre}/detalle")
async def subir_detalle_eje(pid: str, nombre: str, imagen: UploadFile = File(...)):
    """Imagen del detalle de armaduras del sobrecimiento (la exporta Revit con «Eje a análisis»): solo de referencia
    para ingresar las armaduras en la pestaña 7 (no se lee con la API: no tiene costo)."""
    f = _eje_sap(_proyecto(pid), nombre)
    datos = await imagen.read()
    if len(datos) > 8 * 1024 * 1024:
        raise HTTPException(413, "La imagen pesa más de 8 MB")
    _detalle_eje(f, ".detalle.png").write_bytes(datos)
    return {"guardado": True}


@app.get("/api/proyectos/{pid}/sap/{nombre}/detalle")
def ver_detalle_eje(pid: str, nombre: str):
    png = _detalle_eje(_eje_sap(_proyecto(pid), nombre), ".detalle.png")
    if not png.is_file():
        raise HTTPException(404, "Este eje no tiene imagen del detalle de armaduras")
    return FileResponse(png, media_type="image/png", headers={"Cache-Control": "no-store"})


class PedidoFundacion(BaseModel):
    parametros: dict = {}               # ParametrosFundacion (geometría, suelo, materiales) y «armaduras»
    geometria: str | None = None        # modelo 3D: fundaciones exportadas de Revit (nombre); None = líneas automáticas


@app.post("/api/proyectos/{pid}/sap/{nombre}/fundacion")
def fundacion_eje(pid: str, nombre: str, pedido: PedidoFundacion):
    """Fundación corrida del eje con las reacciones de sus apoyos (del análisis guardado): resortes solo en
    compresión, presiones y % de área comprimida, y diseño del pedestal (muro) y de la zapata (viga de fundación)."""
    from app.sap.eje_pynite import VERSION_RESULTADO
    from app.sap.fundacion import ArmadurasFundacion, ParametrosFundacion, analizar_fundacion
    f = _eje_sap(_proyecto(pid), nombre)
    rf = f.with_name(f.name.replace(".sap.json", ".resultado.json"))
    if not rf.is_file():
        raise HTTPException(409, "Primero analiza el eje en la pestaña 6 (las cargas salen de sus apoyos)")
    res = json.loads(rf.read_text(encoding="utf-8"))
    if res.get("version") != VERSION_RESULTADO or not res.get("apoyos"):
        raise HTTPException(409, "El análisis del eje es de una versión anterior: vuelve a analizarlo en la pestaña 6")
    par = dict(pedido.parametros or {})
    arm = par.pop("armaduras", {}) or {}
    try:
        pf = ParametrosFundacion(**{k: v for k, v in par.items() if k in ParametrosFundacion.__dataclass_fields__
                                    and k != "armaduras" and v not in (None, "")})
        pf.armaduras = ArmadurasFundacion(**{k: v for k, v in arm.items() if k in ArmadurasFundacion.__dataclass_fields__
                                             and v not in (None, "")})
        clave = f"{pid}/{nombre}/fundacion"
        _AVANCE_ANALISIS[clave] = {"pct": 0, "etapa": "Preparando", "hasta": 2}
        r = analizar_fundacion(res["apoyos"], res.get("combos_lrfd") or {}, res.get("patrones") or {}, pf,
                               avance=lambda pct, etapa, hasta: _AVANCE_ANALISIS.__setitem__(
                                   clave, {"pct": pct, "etapa": etapa, "hasta": hasta}))
    except (ValueError, TypeError) as e:
        raise HTTPException(400, f"Parámetros no válidos: {e}")
    finally:
        _AVANCE_ANALISIS.pop(f"{pid}/{nombre}/fundacion", None)
    r["fecha"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    r["analisis_eje"] = res.get("fecha")
    texto = json.dumps(r, ensure_ascii=False, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
    f.with_name(f.name.replace(".sap.json", ".fundacion.json")).write_text(texto, encoding="utf-8")
    return Response(texto, media_type="application/json")


@app.get("/api/proyectos/{pid}/sap/{nombre}/fundacion/avance")
def avance_fundacion(pid: str, nombre: str):
    return _AVANCE_ANALISIS.get(f"{pid}/{nombre}/fundacion") or {"pct": None, "etapa": ""}


@app.get("/api/proyectos/{pid}/sap/{nombre}/fundacion")
def fundacion_guardada(pid: str, nombre: str):
    f = _eje_sap(_proyecto(pid), nombre)
    r = f.with_name(f.name.replace(".sap.json", ".fundacion.json"))
    if not r.is_file():
        raise HTTPException(404, "La fundación de este eje aún no se ha analizado")
    return json.loads(r.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ modelos 3D (pestaña 6, modo 3D)
def _modelo3d_f(d: Path, nombre: str) -> Path:
    f = d / "modelos3d" / f"{nombre}.modelo3d.json"
    if not re.fullmatch(r"[\w.\-]+", nombre) or not f.is_file():
        raise HTTPException(404, f"No existe el modelo 3D {nombre}")
    return f


def _hermano3d(f: Path, ext: str) -> Path:
    return f.with_name(f.name.replace(".modelo3d.json", ext))


@app.get("/api/proyectos/{pid}/modelos3d")
def listar_modelos3d(pid: str):
    out = []
    for f in sorted((_proyecto(pid) / "modelos3d").glob("*.modelo3d.json")):
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({"nombre": f.name[: -len(".modelo3d.json")], "proyecto_revit": e.get("proyecto"),
                    "fecha": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                    "pilares": len(e.get("pilares") or []), "vigas": len(e.get("vigas") or []),
                    "muros": len(e.get("muros") or []), "analizado": _hermano3d(f, ".resultado.json").is_file()})
    return out


@app.post("/api/proyectos/{pid}/modelos3d", status_code=201)
async def subir_modelo3d(pid: str, archivo: UploadFile = File(...)):
    """El .modelo3d.json del botón «Modelo 3D a análisis» de Revit (pilares, vigas, diagonales y muros elegidos)."""
    d = _proyecto(pid)
    try:
        e = json.loads((await archivo.read()).decode("utf-8"))
    except Exception:
        raise HTTPException(400, "El archivo no es JSON")
    if e.get("tipo_archivo") != "cad2revit-modelo-3d":
        raise HTTPException(400, "No es un archivo del botón «Modelo 3D a análisis» de Revit")
    nombre = "3d-" + re.sub(r"[^\w.\-]", "_", str(e.get("nombre") or e.get("proyecto") or "revit"))[:60]
    (d / "modelos3d").mkdir(exist_ok=True)
    f = d / "modelos3d" / f"{nombre}.modelo3d.json"
    f.write_text(json.dumps(e, ensure_ascii=False), encoding="utf-8")
    r = _hermano3d(f, ".resultado.json")
    if r.is_file():
        r.unlink()                      # el resultado era de la versión anterior del modelo
    return {"nombre": nombre}


@app.get("/api/proyectos/{pid}/modelos3d/{nombre}")
def leer_modelo3d(pid: str, nombre: str):
    f = _modelo3d_f(_proyecto(pid), nombre)
    a = _hermano3d(f, ".ajustes.json")
    return {"modelo": json.loads(f.read_text(encoding="utf-8")),
            "ajustes": json.loads(a.read_text(encoding="utf-8")) if a.is_file() else {}}


@app.delete("/api/proyectos/{pid}/modelos3d/{nombre}", status_code=204)
def borrar_modelo3d(pid: str, nombre: str):
    f = _modelo3d_f(_proyecto(pid), nombre)
    f.unlink()
    for ext in (".ajustes.json", ".resultado.json", ".fundacion.json"):
        if _hermano3d(f, ext).is_file():
            _hermano3d(f, ext).unlink()


@app.put("/api/proyectos/{pid}/modelos3d/{nombre}/ajustes")
def guardar_ajustes_3d(pid: str, nombre: str, datos: AjustesEje):
    f = _modelo3d_f(_proyecto(pid), nombre)
    _hermano3d(f, ".ajustes.json").write_text(
        json.dumps({"parametros": datos.parametros, "opciones": datos.opciones}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"guardado": True}


def _armar3d(datos: dict, aj: AjustesEje):
    from app.sap.eje_cargas import ParametrosCargas
    from app.sap.eje_revit import ParametrosMuroMadera
    from app.sap.modelo3d import cargas3d, mallar3d
    par = aj.parametros or {}
    try:
        pm = ParametrosMuroMadera(**{k: v for k, v in (par.get("muro") or {}).items()
                                     if k in ParametrosMuroMadera.__dataclass_fields__ and v not in (None, "")})
        mod = mallar3d(datos, pm, malla=float(par.get("malla") or 0.5), apoyo_pilares=par.get("apoyo_pilares") or "rotula",
                       opciones=aj.opciones)
        pc = ParametrosCargas(**{k: v for k, v in (par.get("cargas") or {}).items()
                                 if k in ParametrosCargas.__dataclass_fields__ and v not in (None, "")})
        return mod, pc, cargas3d(mod, pc)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, f"Parámetros no válidos: {e}")


@app.post("/api/proyectos/{pid}/modelos3d/{nombre}/vista")
def vista_modelo3d(pid: str, nombre: str, aj: AjustesEje):
    from app.sap.modelo3d import cargas_para_visor
    f = _modelo3d_f(_proyecto(pid), nombre)
    mod, _, cargas = _armar3d(json.loads(f.read_text(encoding="utf-8")), aj)
    return {
        "nombre": mod.nombre, "nudos": mod.nudos, "apoyos": mod.apoyos, "apoyos_defecto": mod.apoyos_defecto,
        "barras": [{"nombre": b, "ni": a, "nj": c, "seccion": sec, "rol": mod.roles.get(b), "origen": mod.origen.get(b)}
                   for b, a, c, sec in mod.barras],
        "areas": [{"nombre": a, "nudos": ns, "origen": mod.origen.get(a)} for a, ns, _ in mod.areas],
        "muros": [{k: v for k, v in m.items() if k not in ("a", "u")} for m in mod.resumen_muros],
        "secciones": mod.secciones, "cargas": cargas.resumen, "combos": list(cargas.combos), "avisos": mod.avisos,
        "sin_apoyo": mod.sin_apoyo, "excluidos_auto": mod.excluidos_auto, "trib": cargas.trib, "origen_xy": list(mod.origen_xy),
        "liberaciones": {k: list(v) for k, v in mod.liberaciones.items()},
        "superficies": [{k: v for k, v in s_.items() if k in ("id", "tipo", "nombre", "tris")} for s_ in mod.superficies],
        "envolvente": {k: v for k, v in cargas.envolvente.items() if k != "por_barra"},
        "cargas_casos": cargas_para_visor(mod, cargas),
        "conteo": {"nudos": len(mod.nudos), "barras": len(mod.barras), "shells": len(mod.areas)},
    }


@app.post("/api/proyectos/{pid}/modelos3d/{nombre}/analizar")
def analizar_modelo3d(pid: str, nombre: str, aj: AjustesEje):
    """Análisis 3D con PyNite (estático y modal espectral NCh433:2026 en X e Y) y diseño de acero y muros de madera."""
    import numpy as np
    from app.sap.diseno_acero import FY_A270ES
    from app.sap.modelo3d import ModeloInestable, analizar3d
    f = _modelo3d_f(_proyecto(pid), nombre)
    guardar_ajustes_3d(pid, nombre, aj)
    mod, pc, cargas = _armar3d(json.loads(f.read_text(encoding="utf-8")), aj)
    if not mod.barras and not mod.areas:
        raise HTTPException(400, "El modelo no tiene elementos para analizar")
    par = aj.parametros or {}
    clave = f"{pid}/{nombre}/3d"
    _AVANCE_ANALISIS[clave] = {"pct": 0, "etapa": "Preparando", "hasta": 2}
    try:
        dis = par.get("diseno") or {}
        fy = float(dis.get("fy_mpa") or 0) * 1000 / 9.80665 or FY_A270ES
        limites = {k: float(dis[k]) for k in ("viga_total", "lateral_viento", "deriva_sismo") if dis.get(k) not in (None, "")}
        r = analizar3d(mod, cargas, pc, fy=fy, limites=limites or None,
                       avance=lambda pct, etapa, hasta=None: _AVANCE_ANALISIS.__setitem__(
                           clave, {"pct": pct, "etapa": etapa, "hasta": hasta if hasta is not None else pct}))
    except ModeloInestable as e:
        raise HTTPException(400, str(e))
    except np.linalg.LinAlgError:
        raise HTTPException(400, "El modelo 3D es inestable (matriz de rigidez singular): revise los apoyos y las uniones, "
                                 "o saque los elementos sueltos")
    except Exception as e:
        if "singular" in str(e).lower():
            raise HTTPException(400, "El modelo 3D es inestable (matriz de rigidez singular): revise los apoyos y las uniones, "
                                     "o saque los elementos sueltos")
        raise
    finally:
        _AVANCE_ANALISIS.pop(clave, None)
    r["fecha"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    texto = json.dumps(r, ensure_ascii=False, default=lambda o: o.tolist() if hasattr(o, "tolist") else
                       (sorted(o) if isinstance(o, set) else (float(o) if isinstance(o, np.floating) else str(o))))
    _hermano3d(f, ".resultado.json").write_text(texto, encoding="utf-8")
    return Response(texto, media_type="application/json")


def _fund_revit(d: Path, nombre: str) -> Path:
    f = d / "fundaciones3d" / f"{nombre}.fundaciones.json"
    if not re.fullmatch(r"[\w.\-]+", nombre) or not f.is_file():
        raise HTTPException(404, f"No existen las fundaciones de Revit {nombre}")
    return f


@app.get("/api/proyectos/{pid}/fundaciones-revit")
def listar_fundaciones_revit(pid: str):
    out = []
    for f in sorted((_proyecto(pid) / "fundaciones3d").glob("*.fundaciones.json")):
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        cl = [x.get("clase") for x in e.get("elementos") or []]
        out.append({"nombre": f.name[: -len(".fundaciones.json")], "proyecto_revit": e.get("proyecto"),
                    "fecha": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                    "elementos": len(cl), "zapatas_corridas": cl.count("zapata_corrida"), "muros": cl.count("muro"),
                    "zapatas": cl.count("zapata"), "vigas": cl.count("viga"), "losas": cl.count("losa")})
    return out


@app.post("/api/proyectos/{pid}/fundaciones-revit", status_code=201)
async def subir_fundaciones_revit(pid: str, archivo: UploadFile = File(...)):
    """El .fundaciones.json del botón «Fundaciones a análisis» de Revit (geometría de las fundaciones elegidas)."""
    d = _proyecto(pid)
    try:
        e = json.loads((await archivo.read()).decode("utf-8"))
    except Exception:
        raise HTTPException(400, "El archivo no es JSON")
    if e.get("tipo_archivo") != "cad2revit-fundaciones":
        raise HTTPException(400, "No es un archivo del botón «Fundaciones a análisis» de Revit")
    nombre = "fun-" + re.sub(r"[^\w.\-]", "_", str(e.get("nombre") or e.get("proyecto") or "revit"))[:60]
    (d / "fundaciones3d").mkdir(exist_ok=True)
    (d / "fundaciones3d" / f"{nombre}.fundaciones.json").write_text(json.dumps(e, ensure_ascii=False), encoding="utf-8")
    return {"nombre": nombre}


@app.delete("/api/proyectos/{pid}/fundaciones-revit/{nombre}", status_code=204)
def borrar_fundaciones_revit(pid: str, nombre: str):
    _fund_revit(_proyecto(pid), nombre).unlink()


@app.post("/api/proyectos/{pid}/modelos3d/{nombre}/fundacion")
def fundacion_modelo3d(pid: str, nombre: str, pedido: PedidoFundacion):
    """Fundaciones corridas del modelo 3D: los apoyos se agrupan en líneas (alineados en X o en Y) y cada línea se
    analiza como la fundación de un eje (viga sobre resortes de compresión) con las reacciones del análisis 3D."""
    from app.sap.fundacion import ArmadurasFundacion, ParametrosFundacion, analizar_fundacion
    import dataclasses
    from app.sap.fundacion3d import analizar_emparrillado
    from app.sap.modelo3d import lineas_desde_revit, lineas_fundacion
    f = _modelo3d_f(_proyecto(pid), nombre)
    rf = _hermano3d(f, ".resultado.json")
    if not rf.is_file():
        raise HTTPException(400, "Primero analiza el modelo 3D (pestaña 6): sus reacciones son las cargas de la fundación")
    res = json.loads(rf.read_text(encoding="utf-8"))
    if not res.get("apoyos"):
        raise HTTPException(400, "El análisis del modelo 3D es de una versión anterior: vuelve a analizarlo en la pestaña 6")
    par = dict(pedido.parametros or {})
    arm = par.pop("armaduras", None) or {}
    try:
        pf = ParametrosFundacion(**{k: v for k, v in par.items() if k in ParametrosFundacion.__dataclass_fields__ and v not in (None, "")},
                                 armaduras=ArmadurasFundacion(**{k: v for k, v in arm.items()
                                                                 if k in ArmadurasFundacion.__dataclass_fields__ and v not in (None, "")}))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"Parámetros no válidos: {e}")
    avisos = []
    if pedido.geometria:
        fund = json.loads(_fund_revit(_proyecto(pid), pedido.geometria).read_text(encoding="utf-8"))
        lineas, avisos = lineas_desde_revit(res["apoyos"], fund, res.get("origen_xy"))
    else:
        lineas = lineas_fundacion(res["apoyos"])
    clave = f"{pid}/{nombre}/fundacion3d"
    out = []
    conjunto = None
    try:
        if not lineas:
            raise ValueError("El modelo 3D no tiene apoyos")
        # todas las fundaciones juntas, como emparrillado 3D sobre el suelo (unidas en sus cruces); a cada una van las
        # 6 componentes de reacción de sus apoyos
        emp = analizar_emparrillado(lineas, res.get("combos_lrfd") or {}, res.get("patrones") or {}, pf,
                                    avance=lambda pct, etapa, hasta=None: _AVANCE_ANALISIS.__setitem__(
                                        clave, {"pct": pct, "etapa": etapa, "hasta": hasta if hasta is not None else pct}))
        conjunto = emp["conjunto"]
        for l_, r in zip(lineas, emp["lineas"]):
            u = list(l_.get("dir") or (1.0, 0.0))
            A = l_.get("A") or ([0.0, l_["coord"]] if l_["eje"] == "X" else [l_["coord"], 0.0])
            out.append({"nombre": l_["nombre"], "eje": l_["eje"], "coord": l_["coord"], "aislada": bool(l_.get("aislada")),
                        "revit": bool(l_.get("revit")), "ids": l_.get("ids") or [], "A": A, "dir": u,
                        "geometria": l_.get("geometria") or {k: r["seccion"][k] for k in ("b_pedestal", "h_pedestal", "b_zapata", "h_zapata", "profundidad")},
                        "resultado": r})
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        _AVANCE_ANALISIS.pop(clave, None)
    d = {"lineas": out, "fecha": datetime.now(timezone.utc).isoformat(timespec="seconds"), "analisis_3d": res.get("fecha"),
         "geometria": pedido.geometria, "avisos": avisos, "conjunto": conjunto,
         "nota": "Emparrillado 3D: todas las fundaciones juntas, unidas en sus cruces, sobre suelo que solo trabaja en "
                 "compresión (resorte vertical ks·B y al giro transversal ks·B³/12 por metro); cargas: las 6 componentes de "
                 "reacción de cada apoyo del modelo 3D (fuerzas horizontales aplicadas a la altura del pedestal + zapata). La "
                 "presión máxima incluye el vuelco transversal en el ancho de la zapata; el deslizamiento se verifica para el "
                 "conjunto (fuerza horizontal total contra el roce de todas las fundaciones)."}
    texto = json.dumps(d, ensure_ascii=False, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
    _hermano3d(f, ".fundacion.json").write_text(texto, encoding="utf-8")
    return Response(texto, media_type="application/json")


@app.get("/api/proyectos/{pid}/modelos3d/{nombre}/fundacion")
def fundacion_modelo3d_guardada(pid: str, nombre: str):
    r = _hermano3d(_modelo3d_f(_proyecto(pid), nombre), ".fundacion.json")
    if not r.is_file():
        raise HTTPException(404, "Las fundaciones de este modelo 3D aún no se han analizado")
    return json.loads(r.read_text(encoding="utf-8"))


@app.get("/api/proyectos/{pid}/modelos3d/{nombre}/fundacion/avance")
def avance_fundacion_3d(pid: str, nombre: str):
    return _AVANCE_ANALISIS.get(f"{pid}/{nombre}/fundacion3d") or {"pct": None, "etapa": ""}


@app.get("/api/proyectos/{pid}/modelos3d/{nombre}/avance")
def avance_modelo3d(pid: str, nombre: str):
    return _AVANCE_ANALISIS.get(f"{pid}/{nombre}/3d") or {"pct": None, "etapa": ""}


@app.get("/api/proyectos/{pid}/modelos3d/{nombre}/resultado")
def resultado_modelo3d(pid: str, nombre: str):
    r = _hermano3d(_modelo3d_f(_proyecto(pid), nombre), ".resultado.json")
    if not r.is_file():
        raise HTTPException(404, "Este modelo 3D aún no se ha analizado")
    return json.loads(r.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ costaneras (elementos secundarios, pestaña 8)
def _costaneras(d: Path, nombre: str) -> Path:
    f = d / "costaneras" / f"{nombre}.costaneras.json"
    if not re.fullmatch(r"[\w.\-]+", nombre) or not f.is_file():
        raise HTTPException(404, f"No existe el techo {nombre}")
    return f


@app.get("/api/proyectos/{pid}/costaneras")
def listar_costaneras(pid: str):
    d = _proyecto(pid)
    out = []
    for f in sorted((d / "costaneras").glob("*.costaneras.json")):
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({"nombre": f.name[: -len(".costaneras.json")], "proyecto_revit": e.get("proyecto"),
                    "fecha": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"),
                    "sistemas": len(e.get("sistemas") or []), "costaneras": len(e.get("costaneras") or []),
                    "vigas": len(e.get("vigas") or [])})
    return out


@app.post("/api/proyectos/{pid}/costaneras", status_code=201)
async def subir_costaneras(pid: str, archivo: UploadFile = File(...)):
    """El .costaneras.json del botón «Costaneras a análisis» de Revit (sistemas de vigas y vigas de apoyo)."""
    d = _proyecto(pid)
    try:
        e = json.loads((await archivo.read()).decode("utf-8"))
    except Exception:
        raise HTTPException(400, "El archivo no es JSON")
    if e.get("tipo_archivo") != "cad2revit-costaneras":
        raise HTTPException(400, "No es un archivo del botón «Costaneras a análisis» de Revit")
    nombre = "techo-" + re.sub(r"[^\w.\-]", "_", str(e.get("nombre") or e.get("proyecto") or "revit"))[:60]
    (d / "costaneras").mkdir(exist_ok=True)
    (d / "costaneras" / f"{nombre}.costaneras.json").write_text(json.dumps(e, ensure_ascii=False), encoding="utf-8")
    return {"nombre": nombre}


@app.get("/api/proyectos/{pid}/costaneras/{nombre}")
def leer_costaneras(pid: str, nombre: str):
    f = _costaneras(_proyecto(pid), nombre)
    a = f.with_name(f.name.replace(".costaneras.json", ".ajustes.json"))
    return {"datos": json.loads(f.read_text(encoding="utf-8")),
            "ajustes": json.loads(a.read_text(encoding="utf-8")) if a.is_file() else {}}


@app.delete("/api/proyectos/{pid}/costaneras/{nombre}", status_code=204)
def borrar_costaneras(pid: str, nombre: str):
    f = _costaneras(_proyecto(pid), nombre)
    f.unlink()
    for ext in (".ajustes.json", ".resultado.json"):
        a = f.with_name(f.name.replace(".costaneras.json", ext))
        if a.is_file():
            a.unlink()


class PedidoCostaneras(BaseModel):
    parametros: dict = {}


@app.put("/api/proyectos/{pid}/costaneras/{nombre}/ajustes")
def guardar_ajustes_costaneras(pid: str, nombre: str, pedido: PedidoCostaneras):
    f = _costaneras(_proyecto(pid), nombre)
    f.with_name(f.name.replace(".costaneras.json", ".ajustes.json")).write_text(
        json.dumps(pedido.parametros, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


@app.post("/api/proyectos/{pid}/costaneras/{nombre}/analizar")
def analizar_costaneras_ep(pid: str, nombre: str, pedido: PedidoCostaneras):
    """Verificación de las costaneras (AISI S100-16 / NCh427/2) con cargas NCh1537, NCh432 y NCh3171."""
    from app.sap.costaneras import ParametrosCostaneras, analizar_costaneras
    f = _costaneras(_proyecto(pid), nombre)
    datos = json.loads(f.read_text(encoding="utf-8"))
    par = dict(pedido.parametros or {})
    f.with_name(f.name.replace(".costaneras.json", ".ajustes.json")).write_text(json.dumps(par, ensure_ascii=False), encoding="utf-8")
    clave = f"{pid}/{nombre}/costaneras"
    try:
        pc = ParametrosCostaneras(**{k: v for k, v in par.items() if k in ParametrosCostaneras.__dataclass_fields__
                                     and v not in (None, "")})
        _AVANCE_ANALISIS[clave] = {"pct": 0, "etapa": "Preparando", "hasta": 2}
        r = analizar_costaneras(datos, pc, avance=lambda pct, etapa, hasta=None: _AVANCE_ANALISIS.__setitem__(
            clave, {"pct": pct, "etapa": etapa, "hasta": hasta if hasta is not None else pct}))
    except (ValueError, TypeError, KeyError) as e:
        raise HTTPException(400, f"Parámetros no válidos: {e}")
    finally:
        _AVANCE_ANALISIS.pop(clave, None)
    r["fecha"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    texto = json.dumps(r, ensure_ascii=False, default=lambda o: o.tolist() if hasattr(o, "tolist") else
                       (sorted(o) if isinstance(o, set) else str(o)))
    f.with_name(f.name.replace(".costaneras.json", ".resultado.json")).write_text(texto, encoding="utf-8")
    return Response(texto, media_type="application/json")


class PedidoIterar(BaseModel):
    parametros: dict = {}
    candidatos: list[str] = []          # perfiles a probar; vacío = catálogo CA
    tipos: list[str] = []               # tipos de Revit a los que se cambia el perfil; vacío = los de las elegidas


@app.get("/api/costaneras/catalogo")
def catalogo_costaneras():
    from app.sap.costaneras import CATALOGO_CA, peso_perfil
    return [{"perfil": c, "peso_kgf_m": peso_perfil(c)} for c in sorted(CATALOGO_CA, key=peso_perfil)]


@app.post("/api/proyectos/{pid}/costaneras/{nombre}/iterar")
def iterar_costaneras_ep(pid: str, nombre: str, pedido: PedidoIterar):
    """Prueba perfiles en las costaneras elegidas (FU de cada una con cada perfil), para llegar a cumplir."""
    from app.sap.costaneras import CATALOGO_CA, ParametrosCostaneras, iterar_secciones, peso_perfil
    f = _costaneras(_proyecto(pid), nombre)
    datos = json.loads(f.read_text(encoding="utf-8"))
    par = dict(pedido.parametros or {})
    clave = f"{pid}/{nombre}/costaneras"
    cands = pedido.candidatos or sorted(CATALOGO_CA, key=peso_perfil)
    try:
        pc = ParametrosCostaneras(**{k: v for k, v in par.items() if k in ParametrosCostaneras.__dataclass_fields__
                                     and v not in (None, "")})
        _AVANCE_ANALISIS[clave] = {"pct": 0, "etapa": "Preparando", "hasta": 2}
        r = iterar_secciones(datos, pc, cands, pedido.tipos or None, avance=lambda pct, etapa, hasta=None: _AVANCE_ANALISIS.__setitem__(
            clave, {"pct": pct, "etapa": etapa, "hasta": hasta if hasta is not None else pct}))
    except (ValueError, TypeError, KeyError) as e:
        raise HTTPException(400, f"Parámetros no válidos: {e}")
    finally:
        _AVANCE_ANALISIS.pop(clave, None)
    texto = json.dumps(r, ensure_ascii=False, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
    return Response(texto, media_type="application/json")


@app.get("/api/proyectos/{pid}/costaneras/{nombre}/avance")
def avance_costaneras(pid: str, nombre: str):
    return _AVANCE_ANALISIS.get(f"{pid}/{nombre}/costaneras") or {"pct": None, "etapa": ""}


@app.get("/api/proyectos/{pid}/costaneras/{nombre}/resultado")
def resultado_costaneras(pid: str, nombre: str):
    f = _costaneras(_proyecto(pid), nombre)
    r = f.with_name(f.name.replace(".costaneras.json", ".resultado.json"))
    if not r.is_file():
        raise HTTPException(404, "Aún no se han verificado las costaneras de este techo")
    return json.loads(r.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ empresa: logo y firma (comunes a todos los proyectos)
def _empresa_dir() -> Path:
    d = _datos() / "empresa"
    d.mkdir(exist_ok=True)
    return d


def _empresa(con_imagenes: bool = False) -> dict:
    d = _empresa_dir()
    f = d / "empresa.json"
    e = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    for k in ("logo", "firma"):
        img = next((x for x in d.glob(f"{k}.*") if x.suffix.lower() in (".png", ".jpg", ".jpeg")), None)
        e[k] = (img.read_bytes() if con_imagenes else True) if img else (None if con_imagenes else False)
    return e


class DatosEmpresa(BaseModel):
    nombre: str = ""
    calculista: str = ""
    rut: str = ""


@app.get("/api/empresa")
def empresa_datos():
    return _empresa()


@app.put("/api/empresa")
def empresa_guardar(datos: DatosEmpresa):
    (_empresa_dir() / "empresa.json").write_text(json.dumps(datos.model_dump(), ensure_ascii=False), encoding="utf-8")
    return _empresa()


@app.post("/api/empresa/{tipo}")
async def empresa_imagen(tipo: str, archivo: UploadFile = File(...)):
    """Logo de la empresa o firma escaneada (PNG o JPG), para la portada, el encabezado y la firma de las memorias."""
    if tipo not in ("logo", "firma"):
        raise HTTPException(404, "Solo «logo» o «firma»")
    datos = await archivo.read()
    ext = ".png" if datos[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg" if datos[:3] == b"\xff\xd8\xff" else None
    if not ext:
        raise HTTPException(400, "La imagen debe ser PNG o JPG")
    if len(datos) > 5_000_000:
        raise HTTPException(400, "La imagen es muy grande (máx. 5 MB)")
    d = _empresa_dir()
    for x in d.glob(f"{tipo}.*"):
        x.unlink()
    (d / f"{tipo}{ext}").write_bytes(datos)
    return _empresa()


@app.get("/api/empresa/{tipo}")
def empresa_ver_imagen(tipo: str):
    if tipo not in ("logo", "firma"):
        raise HTTPException(404, "Solo «logo» o «firma»")
    img = next((x for x in _empresa_dir().glob(f"{tipo}.*")), None)
    if not img:
        raise HTTPException(404, f"No hay {tipo}")
    return Response(img.read_bytes(), media_type="image/png" if img.suffix == ".png" else "image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.delete("/api/empresa/{tipo}", status_code=204)
def empresa_borrar_imagen(tipo: str):
    if tipo not in ("logo", "firma"):
        raise HTTPException(404, "Solo «logo» o «firma»")
    for x in _empresa_dir().glob(f"{tipo}.*"):
        x.unlink()


# ------------------------------------------------------------------ memoria de cálculo (pestaña 9)
class PedidoMemoria(BaseModel):
    ejes: list[str] = []
    techos: list[str] = []
    opciones: dict = {}


@app.get("/api/proyectos/{pid}/memoria")
def memoria_config(pid: str):
    """Capítulos disponibles, configuraciones rápidas, opciones guardadas y qué resultados hay para la memoria."""
    from app.memoria import CAPITULOS, PRESETS, opciones_por_defecto
    from app.sap.eje_pynite import VERSION_RESULTADO
    d = _proyecto(pid)
    ejes = []
    for f in sorted((d / "sap").glob("*.sap.json")):
        nombre = f.name[: -len(".sap.json")]
        rf, ff = f.with_name(nombre + ".resultado.json"), f.with_name(nombre + ".fundacion.json")
        res = json.loads(rf.read_text(encoding="utf-8")) if rf.is_file() else None
        ejes.append({"nombre": nombre, "analizado": bool(res), "vigente": bool(res and res.get("version") == VERSION_RESULTADO),
                     "fecha": (res or {}).get("fecha"), "fundacion": ff.is_file()})
    techos = []
    for f in sorted((d / "costaneras").glob("*.costaneras.json")):
        nombre = f.name[: -len(".costaneras.json")]
        rf = f.with_name(nombre + ".resultado.json")
        res = json.loads(rf.read_text(encoding="utf-8")) if rf.is_file() else None
        techos.append({"nombre": nombre, "verificado": bool(res), "fecha": (res or {}).get("fecha"),
                       "n": len((res or {}).get("costaneras") or [])})
    g = d / "memoria.json"
    guardadas = json.loads(g.read_text(encoding="utf-8")) if g.is_file() else {}
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    return {"capitulos": [{"clave": k, "titulo": t, "detalles": [{"clave": a, "titulo": b} for a, b in det]} for k, t, det in CAPITULOS],
            "presets": PRESETS, "defecto": opciones_por_defecto(), "guardadas": guardadas, "ejes": ejes, "techos": techos,
            "proyecto": meta.get("nombre", pid)}


@app.put("/api/proyectos/{pid}/memoria")
def memoria_guardar(pid: str, pedido: PedidoMemoria):
    d = _proyecto(pid)
    (d / "memoria.json").write_text(json.dumps(pedido.model_dump(), ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


# ---- imágenes de la memoria: figuras de la app (dibujadas con los resultados), detalle de Revit y subidas
_IMG_TIPOS = {
    "modelo": ("Modelo computacional", "modelo", "Modelo computacional del {n}: barras de acero y muros de marco plataforma como lámina"),
    "modo1": ("Modo fundamental", "modelo", "Forma del modo fundamental del {n}"),
    "diseno": ("Razones de diseño", "acero", "Razones de diseño del {n}: acero (AISC 360-16) y muros de madera"),
    "corte_fun": ("Corte del sobrecimiento", "fundacion", "Corte del sobrecimiento del {n}: geometría y armaduras"),
    "presiones": ("Presiones de contacto", "fundacion", "Presión de contacto bajo la fundación del {n} (combinación de servicio más desfavorable)"),
    "detalle": ("Detalle de armaduras (Revit)", "fundacion", "Detalle de armaduras del sobrecimiento del {n} (Revit)"),
    "planta_cos": ("Planta de costaneras", "costaneras", "Planta de costaneras ({n}): vigas de apoyo, costaneras verificadas y su FU"),
    "momentos_cos": ("Momentos de la costanera más solicitada", "costaneras", "Envolvente de momentos mayorados de la costanera más solicitada ({n})"),
}


def _img_usuario_dir(d: Path) -> Path:
    x = d / "memoria_imgs"
    x.mkdir(exist_ok=True)
    return x


def _imagenes_disponibles(d: Path) -> list[dict]:
    out = []
    for f in sorted((d / "sap").glob("*.sap.json")):
        n = f.name[: -len(".sap.json")]
        etiqueta = n.replace("eje-", "eje ")
        res = f.with_name(n + ".resultado.json").is_file()
        fun = f.with_name(n + ".fundacion.json").is_file()
        for tipo, ok in (("modelo", res), ("modo1", res), ("diseno", res), ("corte_fun", fun), ("presiones", fun),
                         ("detalle", f.with_name(n + ".detalle.png").is_file())):
            if ok:
                t, cap, ley = _IMG_TIPOS[tipo]
                out.append({"id": f"{tipo}|{n}", "titulo": f"{t} · {etiqueta}", "capitulo": cap, "leyenda": ley.format(n=etiqueta),
                            "origen": "revit" if tipo == "detalle" else "app", "fuente": n})
    for f in sorted((d / "costaneras").glob("*.costaneras.json")):
        n = f.name[: -len(".costaneras.json")]
        if f.with_name(n + ".resultado.json").is_file():
            for tipo in ("planta_cos", "momentos_cos"):
                t, cap, ley = _IMG_TIPOS[tipo]
                out.append({"id": f"{tipo}|{n}", "titulo": f"{t} · {n}", "capitulo": cap, "leyenda": ley.format(n=n),
                            "origen": "app", "fuente": n})
    u = _img_usuario_dir(d)
    meta = json.loads((u / "meta.json").read_text(encoding="utf-8")) if (u / "meta.json").is_file() else {}
    for f in sorted(u.iterdir()):
        if f.suffix.lower() in (".png", ".jpg", ".jpeg"):
            m = meta.get(f.name, {})
            out.append({"id": f"usuario|{f.name}", "titulo": m.get("titulo") or f.name, "capitulo": m.get("capitulo") or "antecedentes",
                        "leyenda": m.get("titulo") or "", "origen": "usuario", "fuente": f.name})
    return out


def _imagen_png(d: Path, ident: str) -> bytes:
    from app import figuras as F
    tipo, _, n = ident.partition("|")
    leer = lambda p: json.loads(p.read_text(encoding="utf-8"))
    if tipo == "usuario":
        f = _img_usuario_dir(d) / n
        if not re.fullmatch(r"[\w.\-]+", n) or not f.is_file():
            raise HTTPException(404, "No existe la imagen")
        return f.read_bytes()
    if tipo in ("planta_cos", "momentos_cos"):
        f = _costaneras(d, n)
        res = leer(f.with_name(n + ".resultado.json"))
        return F.planta_costaneras(leer(f), res) if tipo == "planta_cos" else F.momentos_costanera(res)
    if tipo not in _IMG_TIPOS:
        raise HTTPException(404, "Tipo de imagen desconocido")
    f = _eje_sap(d, n)
    if tipo == "detalle":
        return f.with_name(n + ".detalle.png").read_bytes()
    if tipo in ("corte_fun", "presiones"):
        fun = f.with_name(n + ".fundacion.json")
        if not fun.is_file():
            raise HTTPException(404, "El eje no tiene fundación analizada")
        if tipo == "presiones":
            return F.presiones_fundacion(leer(fun))
        return F.corte_fundacion(((_ajustes_eje(f).get("parametros") or {}).get("fundacion")) or {})
    res = leer(f.with_name(n + ".resultado.json"))
    return {"modelo": F.modelo_eje, "modo1": F.modo_eje, "diseno": F.diseno_eje}[tipo](res)


@app.get("/api/proyectos/{pid}/memoria/imagenes")
def memoria_imagenes(pid: str):
    return _imagenes_disponibles(_proyecto(pid))


@app.get("/api/proyectos/{pid}/memoria/imagen")
def memoria_imagen(pid: str, id: str):
    png = _imagen_png(_proyecto(pid), id)
    tipo = "image/jpeg" if png[:3] == b"\xff\xd8\xff" else "image/png"
    return Response(png, media_type=tipo, headers={"Cache-Control": "no-store"})


@app.post("/api/proyectos/{pid}/memoria/imagenes", status_code=201)
async def memoria_subir_imagen(pid: str, archivo: UploadFile = File(...), titulo: str = Form(""), capitulo: str = Form("antecedentes")):
    """Imagen propia para la memoria (PNG o JPG): render, plano, foto, etc., con su leyenda y capítulo."""
    d = _proyecto(pid)
    datos = await archivo.read()
    ext = ".png" if datos[:8] == b"\x89PNG\r\n\x1a\n" else ".jpg" if datos[:3] == b"\xff\xd8\xff" else None
    if not ext:
        raise HTTPException(400, "La imagen debe ser PNG o JPG")
    if len(datos) > 8_000_000:
        raise HTTPException(400, "La imagen es muy grande (máx. 8 MB)")
    u = _img_usuario_dir(d)
    base = re.sub(r"[^\w\-]", "_", Path(archivo.filename or "imagen").stem)[:50] or "imagen"
    nombre, k = base + ext, 1
    while (u / nombre).exists():
        k += 1
        nombre = f"{base}-{k}{ext}"
    (u / nombre).write_bytes(datos)
    mp = u / "meta.json"
    meta = json.loads(mp.read_text(encoding="utf-8")) if mp.is_file() else {}
    meta[nombre] = {"titulo": titulo.strip() or Path(archivo.filename or nombre).stem, "capitulo": capitulo}
    mp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {"id": f"usuario|{nombre}"}


@app.delete("/api/proyectos/{pid}/memoria/imagenes/{nombre}", status_code=204)
def memoria_borrar_imagen(pid: str, nombre: str):
    u = _img_usuario_dir(_proyecto(pid))
    if not re.fullmatch(r"[\w.\-]+", nombre) or not (u / nombre).is_file():
        raise HTTPException(404, "No existe la imagen")
    (u / nombre).unlink()
    mp = u / "meta.json"
    if mp.is_file():
        meta = json.loads(mp.read_text(encoding="utf-8"))
        meta.pop(nombre, None)
        mp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")


def _fuentes_memoria(d: Path, pedido: "PedidoMemoria"):
    leer = lambda f: json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None
    ejes = []
    for nombre in pedido.ejes:
        f = _eje_sap(d, nombre)
        res = leer(f.with_name(nombre + ".resultado.json"))
        if res:
            ejes.append({"nombre": nombre, "eje": leer(f), "ajustes": _ajustes_eje(f), "resultado": res,
                         "fundacion": leer(f.with_name(nombre + ".fundacion.json"))})
    techos = []
    for nombre in pedido.techos:
        f = _costaneras(d, nombre)
        res = leer(f.with_name(nombre + ".resultado.json"))
        if res:
            techos.append({"nombre": nombre, "resultado": res})
    return ejes, techos


@app.post("/api/proyectos/{pid}/memoria/observaciones")
def memoria_observaciones(pid: str, pedido: PedidoMemoria):
    """Observaciones automáticas (temas pendientes o no cubiertos) de la selección, para mostrarlas antes de emitir la
    versión final; «no_cumple» marca las verificaciones que no pasan."""
    from app.memoria import observaciones, opciones_por_defecto
    ejes, techos = _fuentes_memoria(_proyecto(pid), pedido)
    caps = opciones_por_defecto()["capitulos"]
    for k, v in ((pedido.opciones or {}).get("capitulos") or {}).items():
        if k in caps:
            caps[k].update(v if isinstance(v, dict) else {"incluir": bool(v)})
    graves = ("No cumplen", "que no cumplen", "Deslizamiento", "Área en compresión", "Presión de contacto",
              "Estabilidad", "Hormigón de la fundación")
    return [{"titulo": t, "texto": x, "no_cumple": any(g in t for g in graves)} for t, x in observaciones(ejes, techos, caps)]


@app.post("/api/proyectos/{pid}/memoria/word")
def memoria_word(pid: str, pedido: PedidoMemoria):
    """Memoria de cálculo en Word con los ejes, fundaciones y costaneras elegidos y los capítulos activados."""
    from app.memoria import generar_memoria
    d = _proyecto(pid)
    (d / "memoria.json").write_text(json.dumps(pedido.model_dump(), ensure_ascii=False), encoding="utf-8")
    leer = lambda f: json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None
    ejes = []
    for nombre in pedido.ejes:
        f = _eje_sap(d, nombre)
        res = leer(f.with_name(nombre + ".resultado.json"))
        if not res:
            raise HTTPException(409, f"El {nombre} no tiene análisis: analízalo en la pestaña 6")
        ejes.append({"nombre": nombre, "eje": leer(f), "ajustes": _ajustes_eje(f), "resultado": res,
                     "fundacion": leer(f.with_name(nombre + ".fundacion.json"))})
    techos = []
    for nombre in pedido.techos:
        f = _costaneras(d, nombre)
        res = leer(f.with_name(nombre + ".resultado.json"))
        if not res:
            raise HTTPException(409, f"Las costaneras de {nombre} no están verificadas: verifícalas en la pestaña 8")
        techos.append({"nombre": nombre, "resultado": res})
    if not ejes and not techos:
        raise HTTPException(400, "Elige al menos un eje o un techo con resultados")
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    try:
        figuras = []
        for im in (pedido.opciones or {}).get("imagenes") or []:
            if im.get("incluir", True) and im.get("id"):
                figuras.append({"png": _imagen_png(d, im["id"]), "leyenda": im.get("leyenda") or "",
                                "capitulo": im.get("capitulo") or "conclusiones"})
        contenido = generar_memoria(meta.get("nombre", pid), ejes, techos, pedido.opciones or {}, _empresa(True), figuras)
    except (ValueError, TypeError, KeyError) as e:
        raise HTTPException(400, f"No se pudo armar la memoria: {e}")
    nombre_arch = re.sub(r"[^\w.\-]", "_", f"MC_{meta.get('nombre', pid)}.docx")
    return Response(contenido, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f'attachment; filename="{nombre_arch}"'})


class SeccionHormigon(BaseModel):
    b: float                      # mm
    h: float                      # mm
    recubrimiento: float = 40     # mm al centro de la barra
    fc: float = 25                # MPa
    fy: float = 420               # MPa
    as_inf: float = 0             # mm² (tracción en vigas)
    as_sup: float = 0             # mm²
    av: float = 0                 # mm², ramas del estribo
    s: float = 0                  # mm
    capas: list[list[float]] = [] # columna: [[As mm², d mm], ...]
    Mu: float = 0                 # kN·m
    Vu: float = 0                 # kN
    Pu: float = 0                 # kN, + compresión


@app.post("/api/hormigon/verificar")
def verificar_hormigon(sec: SeccionHormigon):
    """Viga o columna rectangular según ACI 318-19: φMn, φVn y, si hay Pu o capas, el diagrama P–M."""
    from app.sap import diseno_hormigon as dh
    if sec.b <= 0 or sec.h <= 0 or sec.fc <= 0 or sec.fy <= 0:
        raise HTTPException(400, "b, h, f'c y fy deben ser positivos")
    d = sec.h - sec.recubrimiento
    out = {}
    if sec.as_inf:
        out["flexion"] = dh.flexion(sec.b, sec.h, d, sec.as_inf, sec.fc, sec.fy, sec.as_sup, sec.recubrimiento)
        out["flexion"]["razon"] = abs(sec.Mu) / out["flexion"]["phiMn_kNm"] if out["flexion"]["phiMn_kNm"] else None
    out["corte"] = dh.corte(sec.b, d, sec.fc, sec.fy, sec.av, sec.s, Nu_kN=max(sec.Pu, 0), Ag=sec.b * sec.h)
    out["corte"]["razon"] = abs(sec.Vu) / out["corte"]["phiVn_kN"] if out["corte"]["phiVn_kN"] else None
    capas = [tuple(c) for c in sec.capas if len(c) == 2 and c[0] > 0]
    if not capas and (sec.Pu or not sec.as_inf) and (sec.as_inf or sec.as_sup):
        capas = [(sec.as_sup, sec.recubrimiento), (sec.as_inf, d)]
    if capas:
        pts = dh.interaccion(sec.b, sec.h, capas, sec.fc, sec.fy)
        out["interaccion"] = {"puntos": pts, **dh.dentro_de_interaccion(pts, sec.Pu, sec.Mu)}
    return out


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
ESTATICOS = RAIZ / "app" / "static"


def _version_web() -> str:
    """Huella del contenido de la interfaz: cambia en cada despliegue que toque la página."""
    h = hashlib.sha1()
    for f in sorted(ESTATICOS.glob("*")):
        h.update(f.name.encode() + f.read_bytes())
    return h.hexdigest()[:10]


VERSION_WEB = _version_web()


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def inicio():
    """La página apunta a los scripts de ESTA versión (v/<huella>/app.js): el navegador no puede
    mezclar una página nueva con un script viejo guardado."""
    html = (ESTATICOS / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="estilos.css"', f'href="v/{VERSION_WEB}/estilos.css"')
    html = html.replace('src="app.js"', f'src="v/{VERSION_WEB}/app.js"')
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/v/{version}/{archivo}", include_in_schema=False)
def estatico_versionado(version: str, archivo: str):
    f = ESTATICOS / _seguro(archivo, "archivo")
    if not f.is_file() or f.name == "index.html":
        raise HTTPException(404, "No existe")
    # la huella va en la ruta: si es la actual se puede guardar para siempre; si no, que no se guarde
    cache = "public, max-age=31536000, immutable" if version == VERSION_WEB else "no-store"
    return FileResponse(f, headers={"Cache-Control": cache})


_CAMBIOS = None


@app.middleware("http")
async def _de_a_uno(request, call_next):
    """Los cambios se procesan de a uno: dos clics seguidos ya no escriben proyecto.yaml al mismo tiempo."""
    global _CAMBIOS
    if request.method in ("GET", "HEAD") or not request.url.path.startswith("/api/"):
        return await call_next(request)
    import asyncio
    if _CAMBIOS is None:
        _CAMBIOS = asyncio.Lock()
    async with _CAMBIOS:
        return await call_next(request)


@app.middleware("http")
async def _sin_cache_estatico(request, call_next):
    """Todo lo que no es la API ni un archivo versionado se revalida siempre."""
    r = await call_next(request)
    ruta = request.url.path
    if not ruta.startswith("/api/") and not ruta.startswith("/v/") and ruta not in ("/", "/index.html"):
        r.headers["Cache-Control"] = "no-cache"
    return r


app.mount("/", StaticFiles(directory=ESTATICOS, html=True), name="static")
