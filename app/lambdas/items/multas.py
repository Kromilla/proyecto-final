"""
multas.py — Multas de los usuarios (rutas /multas y /usuarios/{id}/multas)
─────────────────────────────────────────────────────────────────────────
  POST /multas                 Registrar multa manual   TransactWriteItems + invalidación
  GET  /usuarios/{id}/multas   Multas del usuario       Query por PK + Cache-Aside

Ítem en la tabla (misma partición del usuario):
  PK=USER#<usuarioId>  SK=FINE#<fecha>#<multaId>
  GSI1PK=MULTA#<multaId>  GSI1SK=METADATA
El perfil del usuario lleva el acumulado `saldo_multas`.

Las multas por retraso se generan solas al devolver un libro vencido
(ver prestamos.devolver): MULTA_POR_DIA × días de retraso.
"""
import os
from typing import Any

import cache
import comun
import usuarios
from comun import (
    TABLE_NAME, ErrorApi, Item, RespuestaApi, es_cancelada, leer_json,
    nuevo_id, publico, respuesta, tabla, texto, validar_id,
)
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

MONTO_MINIMO: int = 1_000
MONTO_MAXIMO: int = 500_000
MONTO_MULTIPLO: int = 1_000
MENSAJE_MONTO: str = (
    "El campo 'monto' debe ser un entero entre 1000 y 500000, en múltiplos de 1000"
)


def validar_monto_manual(valor: Any) -> int:
    """Valida el monto de una multa manual: $1.000–$500.000, múltiplos de $1.000."""
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ErrorApi(400, MENSAJE_MONTO)
    if not MONTO_MINIMO <= valor <= MONTO_MAXIMO or valor % MONTO_MULTIPLO != 0:
        raise ErrorApi(400, MENSAJE_MONTO)
    return valor


def multa_por_dia() -> int:
    """Valor de la multa por cada día de retraso (env MULTA_POR_DIA, por defecto 1000)."""
    return int(os.environ.get("MULTA_POR_DIA", 1000))


def nueva_multa(usuario_id: str, monto: int, motivo: str, origen: str,
                prestamo_id: str | None = None) -> Item:
    """Arma el ítem de una multa (lo usan POST /multas y la devolución tardía)."""
    momento = comun.ahora().replace(microsecond=0).isoformat()
    multa_id = nuevo_id()
    multa: Item = {
        "PK": f"USER#{usuario_id}",
        "SK": f"FINE#{momento}#{multa_id}",
        "GSI1PK": f"MULTA#{multa_id}",
        "GSI1SK": "METADATA",
        "tipo": "MULTA",
        "id": multa_id,
        "usuario_id": usuario_id,
        "monto": monto,
        "motivo": motivo,
        "origen": origen,
        "estado": "PENDIENTE",
        "fecha": momento,
    }
    if prestamo_id:
        multa["prestamo_id"] = prestamo_id
    return multa


def invalidar(usuario_id: str) -> None:
    cache.cache_delete(cache.cache_key_usuario(usuario_id),
                       cache.cache_key_multas_usuario(usuario_id))


def crear(event: dict[str, Any]) -> RespuestaApi:
    """POST /multas. Multa manual. La de retraso la crea prestamos.devolver."""
    import prestamos   # import local: prestamos también importa este módulo
    datos = leer_json(event)
    if "usuario_id" not in datos:
        raise ErrorApi(400, "Falta el campo obligatorio 'usuario_id'")
    if "monto" not in datos:
        raise ErrorApi(400, "Falta el campo obligatorio 'monto'")
    usuario_id = validar_id(datos["usuario_id"], "usuario_id")
    monto = validar_monto_manual(datos["monto"])
    motivo = texto(datos, "motivo", obligatorio=True) or ""
    prestamo_id = None
    prestamo = None
    if "prestamo_id" in datos:
        prestamo_id = validar_id(datos["prestamo_id"], "prestamo_id")
        prestamo = prestamos.buscar(prestamo_id)
        if not prestamo:
            raise ErrorApi(404, f"Préstamo '{prestamo_id}' no encontrado")
        if prestamo["usuario_id"] != usuario_id:
            raise ErrorApi(400, "El préstamo no pertenece a ese usuario")
    else:
        if not usuarios.obtener_de_dynamodb(usuario_id):
            raise ErrorApi(404, f"Usuario '{usuario_id}' no encontrado")
        historial = tabla().query(
            KeyConditionExpression=Key("PK").eq(f"USER#{usuario_id}") & Key("SK").begins_with("LOAN#"),
            Limit=1,
        ).get("Items", [])
        if not historial:
            raise ErrorApi(409, "El usuario no tiene préstamos; no se puede registrar una multa")
        # Cualquier préstamo del historial sirve para la condición atómica.
        prestamo = historial[0]

    multa = nueva_multa(usuario_id, monto, motivo, "MANUAL", prestamo_id)
    operaciones: list[dict[str, Any]] = [
        {"Put": {"TableName": TABLE_NAME, "Item": multa,
                 "ConditionExpression": "attribute_not_exists(PK)"}},
        {"Update": {
            "TableName": TABLE_NAME,
            "Key": usuarios.clave_usuario(usuario_id),
            "UpdateExpression": "SET saldo_multas = if_not_exists(saldo_multas, :cero) + :monto",
            "ConditionExpression": "attribute_exists(PK)",
            "ExpressionAttributeValues": {":cero": 0, ":monto": monto},
        }},
    ]
    if prestamo:
        operaciones.append({"ConditionCheck": {
            "TableName": TABLE_NAME,
            "Key": {"PK": prestamo["PK"], "SK": prestamo["SK"]},
            "ConditionExpression": "attribute_exists(PK)",
        }})
    try:
        comun.cliente().transact_write_items(TransactItems=operaciones)
    except ClientError as e:
        if es_cancelada(e):
            if not usuarios.obtener_de_dynamodb(usuario_id):
                raise ErrorApi(404, f"Usuario '{usuario_id}' no encontrado")
            raise ErrorApi(409, "No se pudo registrar la multa; intente de nuevo")
        raise
    invalidar(usuario_id)
    return respuesta(201, publico(multa))


def listar(usuario_id: str) -> RespuestaApi:
    """GET /usuarios/{id}/multas — Query PK=USER#<id>, begins_with(SK,'FINE#')."""
    clave = cache.cache_key_multas_usuario(usuario_id)
    cacheado = cache.cache_get(clave)
    if cacheado is not None:
        return respuesta(200, {**cacheado, "_fuente": "cache"})

    if not usuarios.obtener_de_dynamodb(usuario_id):
        raise ErrorApi(404, f"Usuario '{usuario_id}' no encontrado")
    lista = [publico(m) for m in usuarios.consultar_coleccion(usuario_id, "FINE#")]
    resultado = {
        "usuario_id": usuario_id,
        "total": len(lista),
        "saldo_pendiente": sum(m["monto"] for m in lista if m["estado"] == "PENDIENTE"),
        "multas": lista,
    }
    cache.cache_set(clave, resultado)
    return respuesta(200, {**resultado, "_fuente": "dynamodb"})
