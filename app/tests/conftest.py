"""
conftest.py
───────────
Fixtures compartidos por todos los tests.
Usa moto (mock DynamoDB) y fakeredis (mock Redis) para que los tests
corran SIN el emulador ni Redis reales.
"""
import importlib.util
import os
import sys

import boto3
import fakeredis
import pytest
from moto import mock_aws

DIR_TESTS = os.path.dirname(__file__)
# Agrega lambdas/items al path para poder importar handler y cache
sys.path.insert(0, os.path.join(DIR_TESTS, "../lambdas/items"))

# Dentro de cdk-dev, AWS_ENDPOINT_URL apunta al emulador; si se deja,
# boto3 ignora moto y los tests escribirían en LocalStack real.
os.environ.pop("AWS_ENDPOINT_URL", None)

# Variables de entorno necesarias antes de importar los módulos
os.environ.setdefault("AWS_ACCESS_KEY_ID",     "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_DEFAULT_REGION",    "us-east-1")
os.environ.setdefault("TABLE_NAME",            "test-tabla")
os.environ.setdefault("REDIS_HOST",            "localhost")
os.environ.setdefault("REDIS_PORT",            "6379")
os.environ.setdefault("REDIS_TTL",             "300")
os.environ.setdefault("MAX_PRESTAMOS",         "3")
os.environ.setdefault("DIAS_PRESTAMO",         "14")
os.environ.setdefault("MAX_RENOVACIONES",      "2")
os.environ.setdefault("MULTA_POR_DIA",         "1000")


@pytest.fixture
def tabla_dynamodb():
    """
    Tabla DynamoDB en memoria (moto) con el mismo esquema del stack CDK:
    PK/SK + GSI1, GSI2 y GSI3. Se destruye al terminar cada test.
    """
    with mock_aws():
        cliente = boto3.resource("dynamodb", region_name="us-east-1")
        tabla = cliente.create_table(
            TableName="test-tabla",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
                {"AttributeName": "GSI2PK", "AttributeType": "S"},
                {"AttributeName": "GSI2SK", "AttributeType": "S"},
                {"AttributeName": "GSI3PK", "AttributeType": "S"},
                {"AttributeName": "GSI3SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[{
                "IndexName": "GSI1",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }, {
                "IndexName": "GSI2",
                "KeySchema": [
                    {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }, {
                "IndexName": "GSI3",
                "KeySchema": [
                    {"AttributeName": "GSI3PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI3SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
            BillingMode="PAY_PER_REQUEST",
        )
        tabla.wait_until_exists()
        yield tabla


@pytest.fixture
def redis_fake():
    """Instancia nueva de fakeredis por test (no comparte datos)."""
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def parchear_redis(monkeypatch, redis_fake):
    """Hace que cache.py use fakeredis en lugar del Redis real."""
    import cache
    monkeypatch.setattr(cache, "_get_client", lambda: redis_fake)
    return redis_fake


@pytest.fixture
def health_handler():
    """
    Carga lambdas/health/handler.py con otro nombre de módulo, porque
    lambdas/items también tiene un handler.py.
    """
    ruta = os.path.join(DIR_TESTS, "../lambdas/health/handler.py")
    spec = importlib.util.spec_from_file_location("health_handler", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def llamar(metodo, recurso, id=None, body=None, query=None, headers=None, nombre=None):
    """
    Construye un evento proxy de API Gateway, invoca la Lambda del catálogo
    y retorna (status, cuerpo_json).
    """
    import json
    import handler
    evento = {"httpMethod": metodo, "resource": recurso}
    if id is not None:
        evento["pathParameters"] = {"id": id}
    if nombre is not None:
        evento["pathParameters"] = {"nombre": nombre}
    if query is not None:
        evento["queryStringParameters"] = query
    if body is not None:
        evento["headers"] = {"Content-Type": "application/json"} if headers is None else headers
        evento["body"] = body if isinstance(body, str) else json.dumps(body)
    elif headers is not None:
        evento["headers"] = headers
    respuesta = handler.main(evento, None)
    return respuesta["statusCode"], json.loads(respuesta["body"])


@pytest.fixture
def api(tabla_dynamodb, parchear_redis):
    """Tabla moto + fakeredis listos; retorna la función llamar()."""
    return llamar


@pytest.fixture
def viajar_en_el_tiempo(monkeypatch):
    """
    Cambia la fecha "de hoy" que usan las Lambdas (comun.hoy) para simular
    préstamos vencidos. Uso: viajar_en_el_tiempo(dias=20).
    """
    import comun
    from datetime import timedelta

    def mover(dias):
        destino = comun.ahora().date() + timedelta(days=dias)
        monkeypatch.setattr(comun, "hoy", lambda: destino)
        return destino
    return mover
