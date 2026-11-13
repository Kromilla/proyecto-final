"""
usuarios.py — Usuarios de la biblioteca (rutas /usuarios)
─────────────────────────────────────────────────────────
  POST   /usuarios                   Crear usuario          Write-Through
  GET    /usuarios                   Listar usuarios        Query GSI2 (paginado)
  GET    /usuarios/{id}              Obtener usuario        Cache-Aside
  PUT    /usuarios/{id}              Actualizar usuario     UpdateItem + Write-Through
  DELETE /usuarios/{id}              Eliminar usuario       Invalida claves en Redis
  GET    /usuarios/{id}/prestamos    Préstamos del usuario  Query por PK + Cache-Aside
  (GET   /usuarios/{id}/multas está en multas.py)

Ítems en la tabla (misma partición USER#<id>):
  PK=USER#<id>  SK=PROFILE                                 perfil del usuario
  PK=USER#<id>  SK=LOAN#<fecha>#<itemId>#<prestamoId>      préstamos (prestamos.py)
  PK=USER#<id>  SK=RESV#<itemId>                           reservas  (reservas.py)
  PK=USER#<id>  SK=FINE#<fecha>#<multaId>                  multas    (multas.py)
  GSI2PK=TIPO#USUARIO  GSI2SK=NOMBRE#<nombre>#<id>         listado de usuarios
"""
import logging
import os
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

import cache
from comun import (
    PATRON_EMAIL, ErrorApi, Item, RespuestaApi, ahora_iso, codificar_cursor,
    leer_json, normalizar, nuevo_id, paginar, publico, query_completa, respuesta,
    tabla, texto, validar_id,
)

logger: logging.Logger = logging.getLogger(__name__)
ESTADOS: dict[str, str] = {"activo": "ACTIVO", "devuelto": "DEVUELTO"}
CAMPOS_EDITABLES: frozenset[str] = frozenset({"nombre", "email", "programa"})


def max_prestamos() -> int:
    """Préstamos activos permitidos por usuario (env MAX_PRESTAMOS, por defecto 3)."""
    return int(os.environ.get("MAX_PRESTAMOS", 3))


def clave_usuario(usuario_id: str) -> dict[str, str]:
    return {"PK": f"USER#{usuario_id}", "SK": "PROFILE"}


def obtener_de_dynamodb(usuario_id: str) -> Item | None:
    return tabla().get_item(Key=clave_usuario(usuario_id)).get("Item")


def _validar_email(email: str | None) -> str:
    if not PATRON_EMAIL.match(email or ""):
        raise ErrorApi(400, "El campo 'email' no es un correo válido")
    return (email or "").lower()


def crear(event: dict[str, Any]) -> RespuestaApi:
    """POST /usuarios. Alta con 0 préstamos y saldo 0. 409 si el id existe."""
    datos = leer_json(event)
    nombre = texto(datos, "nombre", obligatorio=True) or ""
    email = _validar_email(texto(datos, "email", obligatorio=True))
    usuario_id = validar_id(datos["id"]) if "id" in datos else nuevo_id()
    usuario: Item = {"id": usuario_id, "nombre": nombre, "email": email}
    programa = texto(datos, "programa")
    if programa:
        usuario["programa"] = programa
    usuario.update(prestamos_activos=0, max_prestamos=max_prestamos(), saldo_multas=0,
                   creado_en=ahora_iso())

    item: Item = {
        **clave_usuario(usuario_id),
        "GSI2PK": "TIPO#USUARIO",
        "GSI2SK": f"NOMBRE#{normalizar(nombre)}#{usuario_id}",
        "tipo": "USUARIO",
        **usuario,
    }
    try:
        tabla().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ErrorApi(409, f"Ya existe un usuario con id '{usuario_id}'")
        raise
    logger.info("DynamoDB PUT usuario=%s", usuario_id)

    salida = publico(item)
    cache.cache_set(cache.cache_key_usuario(usuario_id), salida)   # Write-Through
    return respuesta(201, salida)


def listar(query: dict[str, str]) -> RespuestaApi:
    """GET /usuarios — Query GSI2PK=TIPO#USUARIO, ordenado por nombre, paginado."""
    parametros: dict[str, Any] = {"IndexName": "GSI2",
                                  "KeyConditionExpression": Key("GSI2PK").eq("TIPO#USUARIO")}
    resultado = tabla().query(**paginar(parametros, query))
    lista = [publico(i) for i in resultado.get("Items", [])]
    return respuesta(200, {"usuarios": lista, "total": len(lista),
                           "siguiente": codificar_cursor(resultado.get("LastEvaluatedKey")),
                           "_fuente": "dynamodb"})


def obtener(usuario_id: str) -> RespuestaApi:
    """GET /usuarios/{id} — Cache-Aside."""
    clave = cache.cache_key_usuario(usuario_id)
    cacheado = cache.cache_get(clave)
    if cacheado:
        return respuesta(200, {**cacheado, "_fuente": "cache"})
    item = obtener_de_dynamodb(usuario_id)
    if not item:
        raise ErrorApi(404, f"Usuario '{usuario_id}' no encontrado")
    salida = publico(item)
    cache.cache_set(clave, salida)
    return respuesta(200, {**salida, "_fuente": "dynamodb"})


def actualizar(usuario_id: str, event: dict[str, Any]) -> RespuestaApi:
    """
    PUT /usuarios/{id} — UpdateItem solo de los campos enviados (no pisa
    prestamos_activos ni saldo_multas) + Write-Through con el resultado.
    """
    datos = leer_json(event)
    desconocidos = set(datos) - CAMPOS_EDITABLES
    if desconocidos:
        raise ErrorApi(400, f"Campos no editables: {', '.join(sorted(desconocidos))}")
    if not datos:
        raise ErrorApi(400, "Envíe al menos un campo para actualizar")
    cambios: Item = {}
    for campo in ("nombre", "programa"):
        valor = texto(datos, campo, obligatorio=campo in datos)
        if valor is not None:
            cambios[campo] = valor
    if "email" in datos:
        cambios["email"] = _validar_email(texto(datos, "email", obligatorio=True))

    actual = obtener_de_dynamodb(usuario_id)
    if not actual:
        raise ErrorApi(404, f"Usuario '{usuario_id}' no encontrado")
    nombre = cambios.get("nombre", actual["nombre"])
    cambios["GSI2SK"] = f"NOMBRE#{normalizar(nombre)}#{usuario_id}"
    cambios["actualizado_en"] = ahora_iso()

    nombres = {f"#c{i}": campo for i, campo in enumerate(cambios)}
    try:
        resultado = tabla().update_item(
            Key=clave_usuario(usuario_id),
            UpdateExpression="SET " + ", ".join(f"{n} = :v{i}" for i, n in enumerate(nombres)),
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames=nombres,
            ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(cambios.values())},
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ErrorApi(404, f"Usuario '{usuario_id}' no encontrado")
        raise
    logger.info("DynamoDB UPDATE usuario=%s", usuario_id)

    salida = publico(resultado["Attributes"])
    cache.cache_set(cache.cache_key_usuario(usuario_id), salida)   # Write-Through
    return respuesta(200, salida)


def eliminar(usuario_id: str) -> RespuestaApi:
    """
    DELETE /usuarios/{id} — solo sin préstamos activos ni reservas pendientes.
    Borra toda la partición USER#<id> (perfil + préstamos + multas) e invalida Redis.
    """
    coleccion = query_completa(KeyConditionExpression=Key("PK").eq(f"USER#{usuario_id}"))
    if any(i["SK"].startswith("RESV#") for i in coleccion):
        raise ErrorApi(409, f"El usuario '{usuario_id}' tiene reservas; cancélelas primero")
    try:
        tabla().delete_item(
            Key=clave_usuario(usuario_id),
            ConditionExpression="attribute_exists(PK) AND prestamos_activos = :cero",
            ExpressionAttributeValues={":cero": 0},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        if obtener_de_dynamodb(usuario_id):
            raise ErrorApi(409, f"El usuario '{usuario_id}' tiene préstamos activos")
        raise ErrorApi(404, f"Usuario '{usuario_id}' no encontrado")

    historial = [i for i in coleccion if i["SK"] != "PROFILE"]
    with tabla().batch_writer() as lote:
        for item in historial:
            lote.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    logger.info("DynamoDB DELETE usuario=%s (%d registros)", usuario_id, len(historial))

    cache.cache_delete(cache.cache_key_usuario(usuario_id),
                       cache.cache_key_prestamos_usuario(usuario_id),
                       cache.cache_key_multas_usuario(usuario_id))
    return respuesta(200, {
        "mensaje": f"Usuario '{usuario_id}' eliminado",
        "prestamos_eliminados": sum(1 for i in historial if i["SK"].startswith("LOAN#")),
        "multas_eliminadas": sum(1 for i in historial if i["SK"].startswith("FINE#")),
    })


def tiene_prestamos(usuario_id: str) -> bool:
    """True si el usuario tiene al menos un préstamo (activo, vencido o devuelto)."""
    resultado = tabla().query(
        KeyConditionExpression=Key("PK").eq(f"USER#{usuario_id}") & Key("SK").begins_with("LOAN#"),
        Limit=1,
        Select="COUNT",
    )
    return resultado.get("Count", 0) > 0


def consultar_coleccion(usuario_id: str, prefijo: str) -> list[Item]:
    """Query PK=USER#<id> AND begins_with(SK, prefijo), más recientes primero."""
    return query_completa(
        KeyConditionExpression=Key("PK").eq(f"USER#{usuario_id}") & Key("SK").begins_with(prefijo),
        ScanIndexForward=False,
    )


def listar_prestamos(usuario_id: str, query: dict[str, str]) -> RespuestaApi:
    """GET /usuarios/{id}/prestamos?estado=activo|devuelto — Cache-Aside de la lista."""
    estado = query.get("estado")
    if estado is not None and estado not in ESTADOS:
        raise ErrorApi(400, "'estado' debe ser 'activo' o 'devuelto'")

    clave = cache.cache_key_prestamos_usuario(usuario_id)
    prestamos = cache.cache_get(clave)
    fuente = "cache"
    if prestamos is None:
        fuente = "dynamodb"
        if not obtener_de_dynamodb(usuario_id):
            raise ErrorApi(404, f"Usuario '{usuario_id}' no encontrado")
        prestamos = [publico(p) for p in consultar_coleccion(usuario_id, "LOAN#")]
        cache.cache_set(clave, prestamos)

    if estado:
        prestamos = [p for p in prestamos if p.get("estado") == ESTADOS[estado]]
    return respuesta(200, {"usuario_id": usuario_id, "total": len(prestamos),
                           "prestamos": prestamos, "_fuente": fuente})
