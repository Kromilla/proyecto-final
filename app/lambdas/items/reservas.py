"""
reservas.py — Reservas de libros sin stock (rutas /reservas)
────────────────────────────────────────────────────────────
  POST   /reservas              Reservar un libro agotado   TransactWriteItems + RPUSH
  GET    /items/{id}/reservas   Fila de espera del libro    Query GSI3 (+ LRANGE si coincide)
  DELETE /reservas/{id}         Cancelar la reserva         TransactWriteItems + LREM

Ítem en la tabla:
  PK=USER#<usuarioId>  SK=RESV#<itemId>             (una reserva por usuario y libro)
  GSI1PK=RESERVA#<reservaId>  GSI1SK=METADATA      (buscar una reserva por id)
  GSI3PK=RESERVAS#ITEM#<itemId>  GSI3SK=<fecha>#<reservaId>   (fila del libro, en orden)
El libro lleva el contador `reservas_pendientes`.

Redis: List cola:item#<id> con los ids de reserva en orden de llegada
(RPUSH al reservar, LREM al cancelar o al convertirse en préstamo).
DynamoDB (GSI3) es la fuente de verdad: Redis solo se usa si la List tiene
exactamente los mismos ids, en el mismo orden; si no, se reconstruye.

Regla: un libro con reservas pendientes solo se presta a los primeros N
usuarios de la fila, donde N = ejemplares disponibles (ver prestamos.crear).
"""
import logging
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import cache
import comun
import libros
import usuarios
from comun import (
    TABLE_NAME, ErrorApi, Item, RespuestaApi, es_cancelada, leer_json, nuevo_id,
    publico, query_completa, respuesta, tabla, validar_id,
)

logger: logging.Logger = logging.getLogger(__name__)


def clave_reserva(usuario_id: str, item_id: str) -> dict[str, str]:
    return {"PK": f"USER#{usuario_id}", "SK": f"RESV#{item_id}"}


def buscar(reserva_id: str) -> Item | None:
    items = tabla().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"RESERVA#{reserva_id}"),
    ).get("Items", [])
    return items[0] if items else None


def _reservas_de_item(item_id: str) -> list[Item]:
    """Reservas del libro en el GSI3, ordenadas por fecha_reserva (orden de llegada)."""
    filas = query_completa(
        IndexName="GSI3",
        KeyConditionExpression=Key("GSI3PK").eq(f"RESERVAS#ITEM#{item_id}"),
    )
    filas.sort(key=lambda f: (str(f.get("fecha_reserva", "")), f["id"]))
    return filas


def cola(item_id: str, pendientes: int | None = None, incluir: str | None = None) -> list[str]:
    """
    Fila de reservas del libro (ids en orden). DynamoDB (GSI3) es la fuente de
    verdad. Redis (LRANGE) solo se usa si la List tiene exactamente los mismos
    ids, en el mismo orden; si no, se reconstruye con DEL + RPUSH.

    `incluir` agrega un id recién escrito que el GSI3 todavía podría no mostrar.
    `pendientes` se acepta por compatibilidad; ya no decide si se confía en Redis.
    """
    ids = [f["id"] for f in _reservas_de_item(item_id)]
    if incluir and incluir not in ids:
        ids.append(incluir)
    en_redis = cache.cola_leer(item_id)
    if en_redis is not None and en_redis == ids:
        return en_redis
    logger.info("Cola de %s reconstruida desde DynamoDB (%d reservas)", item_id, len(ids))
    cache.cola_reemplazar(item_id, ids)
    return ids


def listar_de_item(item_id: str) -> RespuestaApi:
    """
    GET /items/{id}/reservas. La fuente de verdad es el GSI3, ordenado por
    fecha_reserva. Redis solo se usa si LRANGE coincide exactamente; si no,
    se reconstruye la List (DEL + RPUSH). Los nombres salen del perfil.
    """
    libro = libros.obtener_de_dynamodb(item_id)
    if not libro:
        raise ErrorApi(404, f"Ítem '{item_id}' no encontrado")

    filas = _reservas_de_item(item_id)
    ids = [f["id"] for f in filas]
    por_id = {f["id"]: f for f in filas}
    en_redis = cache.cola_leer(item_id)
    if en_redis is not None and en_redis == ids:
        fuente = "redis"
    else:
        fuente = "dynamodb"
        cache.cola_reemplazar(item_id, ids)

    lista: list[dict[str, Any]] = []
    for reserva_id in ids:
        reserva = por_id[reserva_id]
        usuario = usuarios.obtener_de_dynamodb(reserva["usuario_id"])
        lista.append({
            "id": reserva["id"],
            "usuario_id": reserva["usuario_id"],
            "nombre": usuario["nombre"] if usuario else reserva["usuario_id"],
            "posicion": len(lista) + 1,
            "fecha_reserva": reserva["fecha_reserva"],
        })
    return respuesta(200, {
        "item_id": item_id,
        "titulo": libro["titulo"],
        "total": len(lista),
        "reservas": lista,
        "_fuente": fuente,
    })


def crear(event: dict[str, Any]) -> RespuestaApi:
    """POST /reservas. Un turno por usuario y libro. La fila queda en GSI3 y en Redis."""
    datos = leer_json(event)
    for campo in ("usuario_id", "item_id"):
        if campo not in datos:
            raise ErrorApi(400, f"Falta el campo obligatorio '{campo}'")
    usuario_id = validar_id(datos["usuario_id"], "usuario_id")
    item_id = validar_id(datos["item_id"], "item_id")

    libro = libros.obtener_de_dynamodb(item_id, consistente=True)
    if not libro:
        raise ErrorApi(404, f"Ítem '{item_id}' no encontrado")
    if int(libro.get("disponibles", 0)) > 0 and int(libro.get("reservas_pendientes", 0)) == 0:
        raise ErrorApi(409, f"Hay ejemplares disponibles de '{item_id}': solicite el préstamo")

    momento = comun.ahora().isoformat()   # con microsegundos: define el orden en la fila
    reserva_id = nuevo_id()
    reserva: Item = {
        **clave_reserva(usuario_id, item_id),
        "GSI1PK": f"RESERVA#{reserva_id}",
        "GSI1SK": "METADATA",
        "GSI3PK": f"RESERVAS#ITEM#{item_id}",
        "GSI3SK": f"{momento}#{reserva_id}",
        "tipo": "RESERVA",
        "id": reserva_id,
        "usuario_id": usuario_id,
        "item_id": item_id,
        "titulo": libro["titulo"],
        "estado": "PENDIENTE",
        "fecha_reserva": momento,
    }
    try:
        comun.cliente().transact_write_items(TransactItems=[
            {"Update": {
                "TableName": TABLE_NAME,
                "Key": libros.clave_libro(item_id),
                "UpdateExpression": "SET reservas_pendientes = if_not_exists(reservas_pendientes, :cero) + :uno",
                "ConditionExpression": ("attribute_exists(PK) AND (disponibles = :cero OR "
                                        "reservas_pendientes > :cero)"),
                "ExpressionAttributeValues": {":uno": 1, ":cero": 0},
            }},
            {"ConditionCheck": {
                "TableName": TABLE_NAME,
                "Key": usuarios.clave_usuario(usuario_id),
                "ConditionExpression": "attribute_exists(PK)",
            }},
            {"Put": {
                "TableName": TABLE_NAME,
                "Item": reserva,
                "ConditionExpression": "attribute_not_exists(PK)",
            }},
        ])
    except ClientError as e:
        if not es_cancelada(e):
            raise
        if not usuarios.obtener_de_dynamodb(usuario_id):
            raise ErrorApi(404, f"Usuario '{usuario_id}' no encontrado")
        if tabla().get_item(Key=clave_reserva(usuario_id, item_id)).get("Item"):
            raise ErrorApi(409, f"El usuario ya tiene una reserva de '{item_id}'")
        raise ErrorApi(409, f"Hay ejemplares disponibles de '{item_id}': solicite el préstamo")
    logger.info("DynamoDB RESERVA %s usuario=%s libro=%s", reserva_id, usuario_id, item_id)

    pendientes = int(libro.get("reservas_pendientes", 0)) + 1
    en_redis = cache.cola_leer(item_id)
    # RPUSH solo si la List ya tenía exactamente las reservas anteriores.
    # Si Redis está vacío, incompleto o falló la lectura, no se omite la
    # reserva: cola(..., incluir=) reconstruye desde DynamoDB y, si el GSI3
    # todavía no muestra el ítem recién escrito, lo agrega.
    if (en_redis is not None
            and reserva_id not in en_redis
            and len(en_redis) == pendientes - 1):
        cache.cola_agregar(item_id, reserva_id)          # RPUSH al final de la fila
    fila = cola(item_id, pendientes, incluir=reserva_id)
    cache.cache_delete(cache.cache_key_item(item_id))    # cambió reservas_pendientes
    posicion = fila.index(reserva_id) + 1 if reserva_id in fila else None
    return respuesta(201, {**publico(reserva), "posicion": posicion})


def cancelar(reserva_id: str) -> RespuestaApi:
    """DELETE /reservas/{id} — borra la reserva, descuenta el contador y LREM."""
    reserva = buscar(reserva_id)
    if not reserva:
        raise ErrorApi(404, f"Reserva '{reserva_id}' no encontrada")
    item_id = reserva["item_id"]
    try:
        comun.cliente().transact_write_items(TransactItems=[
            {"Delete": {
                "TableName": TABLE_NAME,
                "Key": {"PK": reserva["PK"], "SK": reserva["SK"]},
                "ConditionExpression": "GSI1PK = :gsi1",
                "ExpressionAttributeValues": {":gsi1": f"RESERVA#{reserva_id}"},
            }},
            {"Update": {
                "TableName": TABLE_NAME,
                "Key": libros.clave_libro(item_id),
                "UpdateExpression": "SET reservas_pendientes = reservas_pendientes - :uno",
                "ConditionExpression": "attribute_exists(PK) AND reservas_pendientes > :cero",
                "ExpressionAttributeValues": {":uno": 1, ":cero": 0},
            }},
        ])
    except ClientError as e:
        if es_cancelada(e):
            raise ErrorApi(404, f"Reserva '{reserva_id}' no encontrada")
        raise
    logger.info("DynamoDB CANCELAR RESERVA %s", reserva_id)

    cache.cola_quitar(item_id, reserva_id)               # LREM
    cache.cache_delete(cache.cache_key_item(item_id))
    return respuesta(200, {"mensaje": f"Reserva '{reserva_id}' cancelada"})
