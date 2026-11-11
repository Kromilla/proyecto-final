"""
comun.py
────────
Utilidades compartidas por los módulos de la Lambda del catálogo:
acceso a la tabla, respuestas HTTP, parseo del body y validaciones.
"""
import base64
import json
import os
import re
import unicodedata
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, TypeAlias

import boto3
from boto3.resources.base import ServiceResource

RespuestaApi: TypeAlias = dict[str, Any]
Item: TypeAlias = dict[str, Any]

# boto3 lee AWS_ENDPOINT_URL del entorno (el emulador lo inyecta en las Lambdas)
dynamodb: ServiceResource = boto3.resource("dynamodb")
TABLE_NAME: str = os.environ["TABLE_NAME"]

PATRON_ID: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PATRON_EMAIL: re.Pattern[str] = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CLAVES_INTERNAS: frozenset[str] = frozenset(
    {"PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "GSI3PK", "GSI3SK", "titulo_busqueda"})
MAX_LISTA: int = 50


class ErrorApi(Exception):
    """Error controlado que se convierte en una respuesta HTTP."""

    def __init__(self, status: int, mensaje: str) -> None:
        super().__init__(mensaje)
        self.status = status
        self.mensaje = mensaje


def tabla() -> Any:
    return dynamodb.Table(TABLE_NAME)


def ahora() -> datetime:
    return datetime.now(timezone.utc)


def ahora_iso() -> str:
    return ahora().isoformat(timespec="seconds")


def hoy() -> date:
    """Fecha actual (UTC). Los tests la reemplazan para simular vencimientos."""
    return ahora().date()


def cliente() -> Any:
    """
    Cliente del recurso Table, para TransactWriteItems. Acepta valores Python
    (str, int) y boto3 los serializa al formato de DynamoDB.
    """
    return tabla().meta.client


def nuevo_id() -> str:
    return uuid.uuid4().hex[:12]


def desde_dynamo(valor: Any) -> Any:
    """Convierte los Decimal de DynamoDB a int/float (json no los serializa)."""
    if isinstance(valor, Decimal):
        return int(valor) if valor == valor.to_integral_value() else float(valor)
    if isinstance(valor, dict):
        return {k: desde_dynamo(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [desde_dynamo(v) for v in valor]
    return valor


def publico(item: Item) -> Item:
    """Oculta las claves del modelo (PK, SK, GSI*) y convierte Decimal."""
    return desde_dynamo({k: v for k, v in item.items() if k not in CLAVES_INTERNAS})


def normalizar(texto: str) -> str:
    """'Gabriel García Márquez' → 'GABRIEL GARCIA MARQUEZ' (clave estable)."""
    sin_tildes = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return " ".join(sin_tildes.upper().split())


def respuesta(status: int, body: Any) -> RespuestaApi:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            # Permite que el frontend de demo (otro origen) consuma el API
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str, ensure_ascii=False),
    }


def error(status: int, mensaje: str) -> RespuestaApi:
    return respuesta(status, {"error": mensaje})


def leer_json(event: dict[str, Any]) -> dict[str, Any]:
    """Valida Content-Type: application/json y retorna el body como dict."""
    headers = event.get("headers") or {}
    tipo = next((v for k, v in headers.items() if k.lower() == "content-type"), "") or ""
    if "application/json" not in tipo.lower():
        raise ErrorApi(400, "Se requiere el header Content-Type: application/json")
    cuerpo: str | bytes = event.get("body") or ""
    try:
        if event.get("isBase64Encoded"):
            cuerpo = base64.b64decode(cuerpo)
        if isinstance(cuerpo, bytes):
            cuerpo = cuerpo.decode("utf-8")
        datos = json.loads(cuerpo)
    except UnicodeDecodeError:
        raise ErrorApi(400, "El cuerpo debe estar codificado en UTF-8 (revise tildes y eñes)")
    except (json.JSONDecodeError, TypeError, ValueError):
        raise ErrorApi(400, "El cuerpo de la petición no es JSON válido")
    if not isinstance(datos, dict):
        raise ErrorApi(400, "El cuerpo debe ser un objeto JSON")
    return datos


def validar_id(valor: Any, campo: str = "id") -> str:
    if not isinstance(valor, str) or not PATRON_ID.match(valor):
        raise ErrorApi(400, f"El campo '{campo}' solo admite letras, números, '-' y '_' (máx. 64)")
    return valor


def texto(datos: dict[str, Any], campo: str, obligatorio: bool = False) -> str | None:
    valor = datos.get(campo)
    if valor is None:
        if obligatorio:
            raise ErrorApi(400, f"Falta el campo obligatorio '{campo}'")
        return None
    if not isinstance(valor, str):
        raise ErrorApi(400, f"El campo '{campo}' debe ser texto")
    valor = valor.strip()
    if obligatorio and not valor:
        raise ErrorApi(400, f"Falta el campo obligatorio '{campo}'")
    return valor


def entero(valor: Any, campo: str, minimo: int, maximo: int) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or not minimo <= valor <= maximo:
        raise ErrorApi(400, f"El campo '{campo}' debe ser un entero entre {minimo} y {maximo}")
    return valor


def entero_query(query: dict[str, str], nombre: str, defecto: int, maximo: int) -> int:
    crudo = query.get(nombre)
    if crudo is None:
        return defecto
    if not crudo.isdigit() or not 1 <= int(crudo) <= maximo:
        raise ErrorApi(400, f"'{nombre}' debe ser un entero entre 1 y {maximo}")
    return int(crudo)


def codificar_cursor(clave: dict[str, Any] | None) -> str | None:
    """LastEvaluatedKey → token opaco para paginar (?siguiente=)."""
    if not clave:
        return None
    return base64.urlsafe_b64encode(json.dumps(desde_dynamo(clave)).encode()).decode()


def decodificar_cursor(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        clave = json.loads(base64.urlsafe_b64decode(token.encode()).decode())
    except (ValueError, UnicodeDecodeError):
        raise ErrorApi(400, "El parámetro 'siguiente' no es válido")
    if not isinstance(clave, dict):
        raise ErrorApi(400, "El parámetro 'siguiente' no es válido")
    return clave


def paginar(parametros: dict[str, Any], query: dict[str, str], defecto: int = 20) -> dict[str, Any]:
    """Agrega Limit y ExclusiveStartKey (?limite= / ?siguiente=) a una Query."""
    parametros["Limit"] = entero_query(query, "limite", defecto, MAX_LISTA)
    inicio = decodificar_cursor(query.get("siguiente"))
    if inicio:
        parametros["ExclusiveStartKey"] = inicio
    return parametros


def query_completa(**parametros: Any) -> list[Item]:
    """Query que recorre todas las páginas (para colecciones pequeñas)."""
    items: list[Item] = []
    while True:
        resultado = tabla().query(**parametros)
        items.extend(resultado.get("Items", []))
        if "LastEvaluatedKey" not in resultado:
            return items
        parametros["ExclusiveStartKey"] = resultado["LastEvaluatedKey"]


def es_cancelada(e: Exception) -> bool:
    """True si el error es una transacción cancelada por una condición."""
    return getattr(e, "response", {}).get("Error", {}).get("Code") == "TransactionCanceledException"
