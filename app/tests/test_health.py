"""
test_health.py
──────────────
Tests de la Lambda /health: 200 cuando DynamoDB y Redis responden,
503 ("degradado") cuando alguno falla.
"""
import json

import fakeredis
import redis


def test_health_ok(tabla_dynamodb, health_handler, monkeypatch):
    monkeypatch.setattr(health_handler.redis, "Redis",
                        lambda **_k: fakeredis.FakeRedis(decode_responses=True))
    respuesta = health_handler.main({"httpMethod": "GET"}, None)
    cuerpo = json.loads(respuesta["body"])

    assert respuesta["statusCode"] == 200
    assert cuerpo["estado"] == "ok"
    assert cuerpo["dynamodb"]["estado"] == "ok"
    assert cuerpo["redis"]["estado"] == "ok"


def test_health_redis_caido_retorna_503(tabla_dynamodb, health_handler, monkeypatch):
    class RedisCaido:
        def __init__(self, **_k):
            pass

        def ping(self):
            raise redis.ConnectionError("sin conexión")

    monkeypatch.setattr(health_handler.redis, "Redis", RedisCaido)
    respuesta = health_handler.main({"httpMethod": "GET"}, None)
    cuerpo = json.loads(respuesta["body"])

    assert respuesta["statusCode"] == 503
    assert cuerpo["estado"] == "degradado"
    assert cuerpo["dynamodb"]["estado"] == "ok"
    assert cuerpo["redis"]["estado"] == "error"


def test_health_tabla_inexistente_retorna_503(health_handler, monkeypatch):
    from moto import mock_aws
    monkeypatch.setattr(health_handler.redis, "Redis",
                        lambda **_k: fakeredis.FakeRedis(decode_responses=True))
    with mock_aws():  # sin crear la tabla
        respuesta = health_handler.main({"httpMethod": "GET"}, None)
    assert respuesta["statusCode"] == 503
    assert json.loads(respuesta["body"])["dynamodb"]["estado"] == "error"
