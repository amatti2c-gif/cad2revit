"""Uso:  python -m cad2revit entradas/proyecto.yaml -o modelo.json"""
from __future__ import annotations

import argparse
import sys

from .config import ErrorConfiguracion, cargar
from .modelo import construir_modelo, guardar, resumen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cad2revit", description="Convierte planos DXF en un modelo JSON para Revit.")
    ap.add_argument("proyecto", help="ruta a entradas/proyecto.yaml")
    ap.add_argument("-o", "--salida", default="modelo.json", help="archivo JSON de salida")
    ap.add_argument("--validar", action="store_true", help="validar contra schema/modelo.schema.json")
    args = ap.parse_args(argv)

    try:
        modelo = construir_modelo(cargar(args.proyecto))
    except ErrorConfiguracion as e:
        print(e, file=sys.stderr)
        return 2
    if args.validar:
        from .validar import validar
        errores = validar(modelo)
        if errores:
            print("El modelo no cumple el esquema:\n  - " + "\n  - ".join(errores), file=sys.stderr)
            return 3
    guardar(modelo, args.salida)
    print(f"Modelo escrito en {args.salida}  (LOD {modelo['lod']['preset']})")
    print(resumen(modelo))
    for a in modelo["advertencias"]:
        print(f"  ! {a}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
