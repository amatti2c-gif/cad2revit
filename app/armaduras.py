"""Lectura de un croquis de armaduras de pedestal y fundación (foto o imagen) con Claude.

Devuelve las barras en un formato fijo que el complemento de Revit muestra para revisar y luego crea en el muro
(pedestal) y en su zapata corrida (fundación).
"""
from __future__ import annotations

import base64
import json
import os

MODELO = "claude-opus-5-5"

ESQUEMA = {
    "type": "object",
    "properties": {
        "pedestal": {
            "type": "object",
            "properties": {
                "barras_por_cara": {"type": "integer", "description": "barras longitudinales en CADA cara vertical del "
                                    "pedestal, por encima de la fundación (incluye la de la esquina superior)"},
                "diametro_mm": {"type": "number"},
                "estribo_diametro_mm": {"type": "number"},
                "estribo_separacion_cm": {"type": "number"},
                "estribo_hasta_fondo_fundacion": {"type": "boolean", "description": "el estribo del pedestal baja "
                                                  "hasta el fondo de la fundación"},
            },
            "required": ["barras_por_cara", "diametro_mm", "estribo_diametro_mm", "estribo_separacion_cm",
                         "estribo_hasta_fondo_fundacion"],
            "additionalProperties": False,
        },
        "fundacion": {
            "type": "object",
            "properties": {
                "barras_superiores": {"type": "integer", "description": "barras longitudinales en la cara superior "
                                      "de la fundación (incluye las que quedan bajo el pedestal)"},
                "barras_inferiores": {"type": "integer"},
                "diametro_mm": {"type": "number"},
                "estribo_diametro_mm": {"type": "number"},
                "estribo_separacion_cm": {"type": "number"},
            },
            "required": ["barras_superiores", "barras_inferiores", "diametro_mm", "estribo_diametro_mm",
                         "estribo_separacion_cm"],
            "additionalProperties": False,
        },
        "total_barras_longitudinales": {"type": "integer"},
        "observaciones": {"type": "string", "description": "dudas de lectura, en español"},
    },
    "required": ["pedestal", "fundacion", "total_barras_longitudinales", "observaciones"],
    "additionalProperties": False,
}

INSTRUCCIONES = """Eres un ingeniero estructural que lee croquis de armaduras (pueden ser a mano alzada).
La imagen muestra el corte transversal de un sobrecimiento: un pedestal (parte vertical, angosta) sobre una fundación
corrida (parte horizontal, ancha). Las barras longitudinales se ven como puntos; los estribos como rectángulos cerrados.
Notación chilena: «12φ12» = 12 barras de 12 mm; «1Eφ8@20» = un estribo de 8 mm cada 20 cm.

Cuenta los puntos en el dibujo:
- pedestal: barras en cada cara vertical por ENCIMA de la cara superior de la fundación;
- fundación: barras en su cara superior (incluidas las que quedan bajo el pedestal) y en su cara inferior.
Comprueba que pedestal (2 × barras_por_cara) + fundación (superiores + inferiores) coincida con el total indicado en
el texto; si no coincide, prioriza el texto y explica la diferencia en «observaciones». Si el estribo del pedestal se
dibuja bajando hasta el fondo de la fundación, márcalo. Si un dato no aparece, usa el del otro elemento y dilo en
«observaciones». Responde solo con el JSON pedido."""


def leer_croquis(datos: bytes, media_type: str) -> dict:
    """Lee el croquis. Lanza RuntimeError con un mensaje para el usuario si no se puede."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("La app no tiene configurada la clave de la API de Claude (variable ANTHROPIC_API_KEY en "
                           "Railway).")
    if media_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        raise RuntimeError(f"Formato de imagen no soportado ({media_type}); usa JPG o PNG.")
    import anthropic                                    # solo se necesita en esta función

    client = anthropic.Anthropic()
    try:
        r = client.beta.messages.create(
            model=MODELO,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": ESQUEMA}},
            system=INSTRUCCIONES,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                             "data": base64.standard_b64encode(datos).decode("ascii")}},
                {"type": "text", "text": "Lee las armaduras de este croquis de pedestal y fundación."},
            ]}],
        )
    except anthropic.AuthenticationError:
        raise RuntimeError("La clave ANTHROPIC_API_KEY de la app no es válida.")
    except anthropic.RateLimitError:
        raise RuntimeError("La API de Claude está ocupada (límite de uso); intenta de nuevo en un minuto.")
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"La API de Claude respondió con error {e.status_code}.")
    except anthropic.APIConnectionError:
        raise RuntimeError("No se pudo conectar con la API de Claude.")
    if r.stop_reason == "refusal":
        raise RuntimeError("Claude no pudo leer esta imagen.")
    if r.stop_reason == "max_tokens":
        raise RuntimeError("La lectura quedó incompleta; intenta con una imagen más clara.")
    texto = next((b.text for b in r.content if b.type == "text"), "")
    return json.loads(texto)
