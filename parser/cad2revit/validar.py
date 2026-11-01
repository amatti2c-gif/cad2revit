"""Validación del modelo intermedio contra schema/modelo.schema.json."""
from __future__ import annotations

import json
from pathlib import Path

ESQUEMA = Path(__file__).resolve().parents[2] / "schema" / "modelo.schema.json"


def validar(modelo: dict) -> list[str]:
    import jsonschema
    esquema = json.loads(ESQUEMA.read_text(encoding="utf-8"))
    v = jsonschema.Draft202012Validator(esquema)
    return [f"{'/'.join(map(str, e.path)) or '(raíz)'}: {e.message}" for e in v.iter_errors(modelo)]
