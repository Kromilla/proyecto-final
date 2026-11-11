"""
Lambda /health
──────────────
Health check del servicio (catálogo de la biblioteca).
Verifica conectividad con DynamoDB y Redis
y retorna el estado de cada componente.

Rutas soportadas:
  GET    /health         → Estado de DynamoDB y Redis
                           (200 si todo está ok, 503 si alguno falla)
"""
import json
import logging
import os
from typing import Any, TypeAlias

import boto3
import redis
from boto3.resources.base import ServiceResource
from botocore.exceptions import ClientError

# Respuesta en formato proxy de API Gateway
RespuestaApi: TypeAlias = dict[str, Any]
# Estado de un componente: {"estado": "ok" | "error", ...}
EstadoComponente: TypeAlias = dict[str, str]

logger: logging.Logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb: ServiceResource = boto3.resource("dynamodb")
TABLE_NAME: str = os.environ["TABLE_NAME"]


def _check_dynamodb() -> EstadoComponente:
    try:
        tabla: Any = dynamodb.Table(TABLE_NAME)
        tabla.load()   # describe la tabla; falla si no existe
        return {"estado": "ok", "tabla": TABLE_NAME}
    except (ClientError, Exception) as e:
        return {"estado": "error", "detalle": str(e)}


def _check_redis() -> EstadoComponente:
    host: str = os.environ.get("REDIS_HOST", "redis")
    try:
        cliente: redis.Redis = redis.Redis(
            host=host,
            port=int(os.environ.get("REDIS_PORT", 6379)),
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        cliente.ping()
        return {"estado": "ok", "host": host}
    except redis.RedisError as e:
        return {"estado": "error", "detalle": str(e)}


def main(event: dict[str, Any], context: Any) -> RespuestaApi:
    """GET /health. 200 si DynamoDB y Redis responden; 503 si alguno falla."""
    logger.info("Health check solicitado")

    db_status: EstadoComponente    = _check_dynamodb()
    redis_status: EstadoComponente = _check_redis()

    todo_ok: bool = (
        db_status["estado"] == "ok"
        and redis_status["estado"] == "ok"
    )

    return {
        "statusCode": 200 if todo_ok else 503,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps({
            "estado":   "ok" if todo_ok else "degradado",
            "dynamodb": db_status,
            "redis":    redis_status,
        }),
    }
