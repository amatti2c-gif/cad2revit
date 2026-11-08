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
import hashlib
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
            ruta.write_text(buf.getvalue(), encoding="utf-8")
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
    py.write_text(texto, encoding="utf-8")
    return {"guardado": True, "errores": _validar_entradas(d)}


# ---------------------------------------------------------------- estructuración por tramos
OPCIONES_TRAMO = {"auto", "muro", "momento", "arriostrado", "ninguno"}


class EleccionesTramos(BaseModel):
    elecciones: dict[str, str] = {}


@app.get("/api/proyectos/{pid}/tramos")
def leer_tramos(pid: str):
    d = _proyecto(pid)
    return {"elecciones": _leer_yaml_seguro(d / "entradas" / "proyecto.yaml").get("tramos") or {}}


@app.put("/api/proyectos/{pid}/tramos")
def guardar_tramos(pid: str, datos: EleccionesTramos):
    """Guarda en proyecto.yaml las tipologías elegidas por tramo de eje ("A:1-2") o por tramo
    y piso ("A:1-2@N2"). "auto" (o no estar) = usar la propuesta de la app."""
    d = _proyecto(pid)
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
    py.write_text(texto, encoding="utf-8")
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
    py.write_text(texto, encoding="utf-8")
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
    py.write_text(texto, encoding="utf-8")
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
        py.write_text(texto, encoding="utf-8")
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
    py.write_text(texto, encoding="utf-8")
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


@app.middleware("http")
async def _sin_cache_estatico(request, call_next):
    """Todo lo que no es la API ni un archivo versionado se revalida siempre."""
    r = await call_next(request)
    ruta = request.url.path
    if not ruta.startswith("/api/") and not ruta.startswith("/v/") and ruta not in ("/", "/index.html"):
        r.headers["Cache-Control"] = "no-cache"
    return r


app.mount("/", StaticFiles(directory=ESTATICOS, html=True), name="static")
