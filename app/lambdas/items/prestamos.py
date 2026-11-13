"""
prestamos.py — Préstamos de libros (rutas /prestamos)
─────────────────────────────────────────────────────
  POST /prestamos                  Prestar un libro       TransactWriteItems + invalidación
  GET  /prestamos?estado=          Préstamos por estado   Query GSI3 (paginado)
  GET  /prestamos/vencidos         Activos ya vencidos    Query GSI3 con GSI3SK < hoy
  GET  /prestamos/{id}             Obtener préstamo       Query GSI1
  PUT  /prestamos/{id}/devolver    Devolver el libro      TransactWriteItems (+ multa si hay retraso)
  PUT  /prestamos/{id}/renovar     Renovar el préstamo    TransactWriteItems + invalidación

Ítem en la tabla:
  PK=USER#<usuarioId>  SK=LOAN#<fecha>#<itemId>#<prestamoId>   (préstamos de un usuario)
  GSI1PK=PRESTAMO#<prestamoId>   GSI1SK=METADATA              (préstamo por id)
  GSI3PK=PRESTAMOS#<ESTADO>      GSI3SK=<fecha_limite>#<id>   (por estado, ordenado por vencimiento)

Un préstamo modifica varios ítems de forma atómica (todo o nada):
  libro   → disponibles - 1, prestados + 1     (condición: disponibles > 0)
  usuario → prestamos_activos + 1              (condición: < MAX_PRESTAMOS)
  préstamo→ se crea con estado ACTIVO
  reserva → si el usuario tenía turno en la fila, la reserva se consume
"""
import logging
import os
from datetime import date, timedelta
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import cache
import comun
import libros
import multas
import reservas
import usuarios
from comun import (
    TABLE_NAME, ErrorApi, Item, RespuestaApi, codificar_cursor, es_cancelada,
    leer_json, nuevo_id, paginar, publico, respuesta, tabla, validar_id,
)

logger: logging.Logger = logging.getLogger(__name__)
ESTADOS: dict[str, str] = {"activo": "ACTIVO", "devuelto": "DEVUELTO"}


def dias_prestamo() -> int:
    """Plazo del préstamo y de cada renovación (env DIAS_PRESTAMO, por defecto 14)."""
    return int(os.environ.get("DIAS_PRESTAMO", 14))


def max_renovaciones() -> int:
    return int(os.environ.get("MAX_RENOVACIONES", 2))


def _invalidar(usuario_id: str, item_id: str) -> None:
    """El stock del libro y los datos del usuario cambiaron: se borran de Redis."""
    cache.cache_delete(
        cache.cache_key_item(item_id),
        cache.cache_key_usuario(usuario_id),
        cache.cache_key_prestamos_usuario(usuario_id),
    )


def _dias_retraso(prestamo: Item, referencia: date) -> int:
    return max(0, (referencia - date.fromisoformat(prestamo["fecha_limite"])).days)


def buscar(prestamo_id: str) -> Item | None:
    items = tabla().query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"PRESTAMO#{prestamo_id}"),
    ).get("Items", [])
    return items[0] if items else None


def _buscar_o_404(prestamo_id: str) -> Item:
    prestamo = buscar(prestamo_id)
    if not prestamo or prestamo.get("tipo") != "PRESTAMO":
        raise ErrorApi(404, f"Préstamo '{prestamo_id}' no encontrado")
    return prestamo


# ── POST /prestamos ───────────────────────────────────────────────────

def clave_prestamo_activo(usuario_id: str, item_id: str) -> dict[str, str]:
    """Ítem que impide un segundo préstamo activo del mismo libro al mismo usuario."""
    return {"PK": f"USER#{usuario_id}", "SK": f"LOANACTIVO#{item_id}"}


def crear(event: dict[str, Any]) -> RespuestaApi:
    """POST /prestamos. Baja el stock y, si hay fila, solo entrega a los primeros N."""
    clave_idem, repetida = comun.respuesta_idempotente(event, "prestamos")
    if repetida:
        return repetida

    datos = leer_json(event)
    for campo in ("usuario_id", "item_id"):
        if campo not in datos:
            raise ErrorApi(400, f"Falta el campo obligatorio '{campo}'")
    usuario_id = validar_id(datos["usuario_id"], "usuario_id")
    item_id = validar_id(datos["item_id"], "item_id")

    libro = libros.obtener_de_dynamodb(item_id)
    if not libro:
        raise ErrorApi(404, f"Ítem '{item_id}' no encontrado")

    # ¿Hay fila de reservas? Solo prestan los primeros N (N = disponibles).
    pendientes = int(libro.get("reservas_pendientes", 0))
    disponibles = int(libro.get("disponibles", 0))
    reserva: Item | None = None
    if pendientes > 0 and disponibles > 0:
        reserva = tabla().get_item(Key=reservas.clave_reserva(usuario_id, item_id)).get("Item")
        fila = reservas.cola(item_id, pendientes)
        if not reserva or reserva["id"] not in fila[:disponibles]:
            raise ErrorApi(409, f"'{item_id}' está reservado para otros usuarios en la fila")

    if tabla().get_item(Key=clave_prestamo_activo(usuario_id, item_id)).get("Item"):
        raise ErrorApi(409, "El usuario ya tiene un préstamo activo de este libro")

    momento = comun.ahora().replace(microsecond=0)
    prestamo_id = nuevo_id()
    fecha_limite = (momento.date() + timedelta(days=dias_prestamo())).isoformat()
    prestamo: Item = {
        "PK": f"USER#{usuario_id}",
        "SK": f"LOAN#{momento.isoformat()}#{item_id}#{prestamo_id}",
        "GSI1PK": f"PRESTAMO#{prestamo_id}",
        "GSI1SK": "METADATA",
        "GSI3PK": "PRESTAMOS#ACTIVO",
        "GSI3SK": f"{fecha_limite}#{prestamo_id}",
        "tipo": "PRESTAMO",
        "id": prestamo_id,
        "usuario_id": usuario_id,
        "item_id": item_id,
        "titulo": libro["titulo"],      # desnormalizado: el historial no necesita leer el libro
        "estado": "ACTIVO",
        "fecha_prestamo": momento.isoformat(),
        "fecha_limite": fecha_limite,
        "renovaciones": 0,
    }

    actualizar_libro = "SET disponibles = disponibles - :uno, prestados = prestados + :uno"
    condicion_libro = "attribute_exists(PK) AND disponibles > :cero"
    if reserva:
        actualizar_libro += ", reservas_pendientes = reservas_pendientes - :uno"
    else:
        condicion_libro += (" AND (attribute_not_exists(reservas_pendientes)"
                            " OR reservas_pendientes = :cero)")
    operaciones: list[dict[str, Any]] = [
        {"Update": {
            "TableName": TABLE_NAME,
            "Key": libros.clave_libro(item_id),
            "UpdateExpression": actualizar_libro,
            "ConditionExpression": condicion_libro,
            "ExpressionAttributeValues": {":uno": 1, ":cero": 0},
        }},
        {"Update": {
            "TableName": TABLE_NAME,
            "Key": usuarios.clave_usuario(usuario_id),
            "UpdateExpression": "SET prestamos_activos = prestamos_activos + :uno",
            "ConditionExpression": ("attribute_exists(PK) AND prestamos_activos < :max AND "
                                    "(attribute_not_exists(saldo_multas) OR saldo_multas <= :cero)"),
            "ExpressionAttributeValues": {":uno": 1, ":max": usuarios.max_prestamos(), ":cero": 0},
        }},
        {"Put": {
            "TableName": TABLE_NAME,
            "Item": prestamo,
            "ConditionExpression": "attribute_not_exists(PK)",
        }},
    ]
    if reserva:
        operaciones.append({"Delete": {
            "TableName": TABLE_NAME,
            "Key": {"PK": reserva["PK"], "SK": reserva["SK"]},
            "ConditionExpression": "attribute_exists(PK)",
        }})
    operaciones.append({"Put": {
        "TableName": TABLE_NAME,
        "Item": {**clave_prestamo_activo(usuario_id, item_id),
                 "tipo": "LOANACTIVO", "prestamo_id": prestamo_id, "item_id": item_id},
        "ConditionExpression": "attribute_not_exists(PK)",
    }})
    if clave_idem:
        operaciones.append(comun.put_idempotencia("prestamos", clave_idem, 201, publico(prestamo)))
    try:
        comun.cliente().transact_write_items(TransactItems=operaciones)
    except ClientError as e:
        if not es_cancelada(e):
            raise
        if clave_idem:
            repetida = comun.recuperar_idempotencia("prestamos", clave_idem)
            if repetida:
                return repetida
        if tabla().get_item(Key=clave_prestamo_activo(usuario_id, item_id)).get("Item"):
            raise ErrorApi(409, "El usuario ya tiene un préstamo activo de este libro")
        _explicar_rechazo(usuario_id, item_id)
    logger.info("DynamoDB PRESTAMO %s usuario=%s libro=%s", prestamo_id, usuario_id, item_id)

    if reserva:
        cache.cola_quitar(item_id, reserva["id"])       # LREM: el turno se atendió
    _invalidar(usuario_id, item_id)
    cache.ranking_incrementar(item_id, cache.CLAVE_RANKING_PRESTAMOS)
    return respuesta(201, publico(prestamo))


def _explicar_rechazo(usuario_id: str, item_id: str) -> None:
    """La transacción se canceló: se averigua qué condición falló."""
    usuario = usuarios.obtener_de_dynamodb(usuario_id)
    if not usuario:
        raise ErrorApi(404, f"Usuario '{usuario_id}' no encontrado")
    libro = libros.obtener_de_dynamodb(item_id)
    if not libro:
        raise ErrorApi(404, f"Ítem '{item_id}' no encontrado")
    if int(usuario.get("saldo_multas", 0)) > 0:
        raise ErrorApi(409, "El usuario tiene multas pendientes; no se puede prestar hasta saldarlas")
    if int(libro.get("disponibles", 0)) <= 0:
        raise ErrorApi(409, f"No hay ejemplares disponibles de '{item_id}'; puede reservarlo")
    if int(usuario.get("prestamos_activos", 0)) >= usuarios.max_prestamos():
        raise ErrorApi(409, f"El usuario ya tiene {usuarios.max_prestamos()} préstamos activos")
    raise ErrorApi(409, "El préstamo no se pudo registrar; intente de nuevo")


# ── Consultas ─────────────────────────────────────────────────────────

def listar(query: dict[str, str]) -> RespuestaApi:
    """GET /prestamos?estado=activo|devuelto — Query GSI3, por fecha límite."""
    estado = query.get("estado", "activo")
    if estado not in ESTADOS:
        raise ErrorApi(400, "'estado' debe ser 'activo' o 'devuelto'")
    parametros: dict[str, Any] = {
        "IndexName": "GSI3",
        "KeyConditionExpression": Key("GSI3PK").eq(f"PRESTAMOS#{ESTADOS[estado]}"),
    }
    resultado = tabla().query(**paginar(parametros, query))
    lista = [publico(p) for p in resultado.get("Items", [])]
    return respuesta(200, {"estado": ESTADOS[estado], "total": len(lista), "prestamos": lista,
                           "siguiente": codificar_cursor(resultado.get("LastEvaluatedKey")),
                           "_fuente": "dynamodb"})


def vencidos(query: dict[str, str]) -> RespuestaApi:
    """
    GET /prestamos/vencidos — préstamos ACTIVOS cuya fecha límite ya pasó:
    Query GSI3PK=PRESTAMOS#ACTIVO AND GSI3SK < '<hoy>' (la SK empieza con la
    fecha límite ISO, así que el orden de texto es el orden cronológico).
    """
    hoy = comun.hoy()
    parametros: dict[str, Any] = {
        "IndexName": "GSI3",
        "KeyConditionExpression": Key("GSI3PK").eq("PRESTAMOS#ACTIVO") & Key("GSI3SK").lt(hoy.isoformat()),
    }
    resultado = tabla().query(**paginar(parametros, query))
    lista = [{**publico(p), "dias_retraso": _dias_retraso(p, hoy),
              "multa_estimada": _dias_retraso(p, hoy) * multas.multa_por_dia()}
             for p in resultado.get("Items", [])]
    return respuesta(200, {"fecha": hoy.isoformat(), "total": len(lista), "prestamos": lista,
                           "siguiente": codificar_cursor(resultado.get("LastEvaluatedKey")),
                           "_fuente": "dynamodb"})


def obtener(prestamo_id: str) -> RespuestaApi:
    prestamo = _buscar_o_404(prestamo_id)
    return respuesta(200, {**publico(prestamo), "_fuente": "dynamodb"})


# ── PUT /prestamos/{id}/devolver ──────────────────────────────────────

def devolver(prestamo_id: str) -> RespuestaApi:
    """
    Marca el préstamo como DEVUELTO, devuelve el ejemplar al stock y, si se
    entregó después de la fecha límite, crea una multa automática
    (días de retraso × MULTA_POR_DIA) en la misma transacción.
    """
    prestamo = _buscar_o_404(prestamo_id)
    if prestamo["estado"] != "ACTIVO":
        raise ErrorApi(409, f"El préstamo '{prestamo_id}' ya fue devuelto")

    usuario_id, item_id = prestamo["usuario_id"], prestamo["item_id"]
    fecha = comun.ahora().replace(microsecond=0).isoformat()
    retraso = _dias_retraso(prestamo, comun.hoy())
    multa: Item | None = None
    if retraso > 0:
        multa = multas.nueva_multa(usuario_id, retraso * multas.multa_por_dia(),
                                   f"Devolución con {retraso} día(s) de retraso",
                                   "AUTOMATICA", prestamo_id)

    operaciones: list[dict[str, Any]] = [
        {"Update": {
            "TableName": TABLE_NAME,
            "Key": {"PK": prestamo["PK"], "SK": prestamo["SK"]},
            "UpdateExpression": "SET estado = :devuelto, fecha_devolucion = :fecha, GSI3PK = :gsi3pk",
            "ConditionExpression": "estado = :activo",
            "ExpressionAttributeValues": {":devuelto": "DEVUELTO", ":activo": "ACTIVO",
                                          ":fecha": fecha, ":gsi3pk": "PRESTAMOS#DEVUELTO"},
        }},
        {"Update": {
            "TableName": TABLE_NAME,
            "Key": usuarios.clave_usuario(usuario_id),
            "UpdateExpression": ("SET prestamos_activos = prestamos_activos - :uno, "
                                 "saldo_multas = if_not_exists(saldo_multas, :cero) + :monto"),
            "ConditionExpression": "attribute_exists(PK)",
            "ExpressionAttributeValues": {":uno": 1, ":cero": 0,
                                          ":monto": multa["monto"] if multa else 0},
        }},
        {"Update": {
            "TableName": TABLE_NAME,
            "Key": libros.clave_libro(item_id),
            "UpdateExpression": "SET disponibles = disponibles + :uno, prestados = prestados - :uno",
            "ConditionExpression": "attribute_exists(PK)",
            "ExpressionAttributeValues": {":uno": 1},
        }},
        {"Delete": {
            "TableName": TABLE_NAME,
            "Key": clave_prestamo_activo(usuario_id, item_id),
        }},
    ]
    if multa:
        operaciones.append({"Put": {"TableName": TABLE_NAME, "Item": multa,
                                    "ConditionExpression": "attribute_not_exists(PK)"}})
    try:
        comun.cliente().transact_write_items(TransactItems=operaciones)
    except ClientError as e:
        if es_cancelada(e):
            raise ErrorApi(409, f"El préstamo '{prestamo_id}' ya fue devuelto")
        raise
    logger.info("DynamoDB DEVOLUCION %s (retraso=%d)", prestamo_id, retraso)

    _invalidar(usuario_id, item_id)
    if multa:
        multas.invalidar(usuario_id)
    libro = libros.obtener_de_dynamodb(item_id) or {}
    return respuesta(200, {
        **publico(prestamo), "estado": "DEVUELTO", "fecha_devolucion": fecha,
        "dias_retraso": retraso,
        "multa": publico(multa) if multa else None,
        "reservas_en_espera": int(libro.get("reservas_pendientes", 0)),
    })


# ── PUT /prestamos/{id}/renovar ───────────────────────────────────────

def renovar(prestamo_id: str) -> RespuestaApi:
    """
    Extiende la fecha límite DIAS_PRESTAMO días más. Reglas:
      - solo préstamos ACTIVOS y no vencidos (vencido → devolver y pagar multa),
      - máximo MAX_RENOVACIONES (2) renovaciones,
      - no se renueva si el libro tiene reservas pendientes (otros esperan).
    Las reglas se verifican primero para dar un mensaje claro y se repiten
    como condiciones de la transacción para que no haya carreras.
    """
    prestamo = _buscar_o_404(prestamo_id)
    hoy = comun.hoy()
    if prestamo["estado"] != "ACTIVO":
        raise ErrorApi(409, f"El préstamo '{prestamo_id}' ya fue devuelto")
    if _dias_retraso(prestamo, hoy) > 0:
        raise ErrorApi(409, "El préstamo está vencido: no se puede renovar")
    renovaciones = int(prestamo.get("renovaciones", 0))
    if renovaciones >= max_renovaciones():
        raise ErrorApi(409, f"El préstamo ya se renovó {max_renovaciones()} veces")
    libro = libros.obtener_de_dynamodb(prestamo["item_id"]) or {}
    if int(libro.get("reservas_pendientes", 0)) > 0:
        raise ErrorApi(409, "El libro tiene reservas pendientes: no se puede renovar")

    nueva = (date.fromisoformat(prestamo["fecha_limite"]) + timedelta(days=dias_prestamo())).isoformat()
    try:
        comun.cliente().transact_write_items(TransactItems=[
            {"ConditionCheck": {
                "TableName": TABLE_NAME,
                "Key": libros.clave_libro(prestamo["item_id"]),
                "ConditionExpression": ("attribute_not_exists(reservas_pendientes) OR "
                                        "reservas_pendientes = :cero"),
                "ExpressionAttributeValues": {":cero": 0},
            }},
            {"Update": {
                "TableName": TABLE_NAME,
                "Key": {"PK": prestamo["PK"], "SK": prestamo["SK"]},
                "UpdateExpression": ("SET fecha_limite = :nueva, GSI3SK = :gsi3sk, "
                                     "renovaciones = if_not_exists(renovaciones, :cero) + :uno"),
                "ConditionExpression": ("estado = :activo AND fecha_limite >= :hoy AND "
                                        "(attribute_not_exists(renovaciones) OR renovaciones < :max)"),
                "ExpressionAttributeValues": {
                    ":nueva": nueva, ":gsi3sk": f"{nueva}#{prestamo_id}", ":cero": 0, ":uno": 1,
                    ":activo": "ACTIVO", ":hoy": hoy.isoformat(), ":max": max_renovaciones(),
                },
            }},
        ])
    except ClientError as e:
        if es_cancelada(e):
            raise ErrorApi(409, "El préstamo cambió mientras se renovaba; intente de nuevo")
        raise
    logger.info("DynamoDB RENOVACION %s → %s", prestamo_id, nueva)

    cache.cache_delete(cache.cache_key_prestamos_usuario(prestamo["usuario_id"]))
    return respuesta(200, {**publico(prestamo), "fecha_limite": nueva,
                           "renovaciones": renovaciones + 1})
