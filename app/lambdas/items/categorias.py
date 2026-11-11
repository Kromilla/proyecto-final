"""
categorias.py — Categorías del catálogo (rutas /categorias)
───────────────────────────────────────────────────────────
  GET /categorias                   Nombres de categorías   Cache-Aside sobre un Set de Redis
  GET /categorias/{nombre}/items    Libros de la categoría  Query sobre GSI3

Ítem en la tabla (lo mantiene libros.py dentro de sus transacciones):
  PK=CATEGORIA#<NOMBRE>  SK=METADATA  nombre, libros (contador)
  GSI2PK=TIPO#CATEGORIA  GSI2SK=CATEGORIA#<NOMBRE>
Los libros de una categoría se leen por GSI3PK=CATEGORIA#<NOMBRE>.
"""
from typing import Any
from urllib.parse import unquote

from boto3.dynamodb.conditions import Key

import cache
from comun import (
    ErrorApi, RespuestaApi, codificar_cursor, normalizar, paginar, publico,
    query_completa, respuesta, tabla,
)
from libros import clave_categoria, libro_publico


def listar() -> RespuestaApi:
    """GET /categorias — SMEMBERS categorias; si no está, Query GSI2 y SADD."""
    nombres = cache.categorias_get()
    if nombres is not None:
        return respuesta(200, {"total": len(nombres), "categorias": nombres, "_fuente": "cache"})

    items = query_completa(IndexName="GSI2",
                           KeyConditionExpression=Key("GSI2PK").eq("TIPO#CATEGORIA"))
    # Una categoría queda en la tabla con libros=0 si se borran todos sus libros
    nombres = [i["nombre"] for i in items if int(i.get("libros", 0)) > 0]
    cache.categorias_set(nombres)
    return respuesta(200, {"total": len(nombres), "categorias": sorted(nombres),
                           "_fuente": "dynamodb"})


def libros_de(nombre: str, query: dict[str, str]) -> RespuestaApi:
    """GET /categorias/{nombre}/items — Query GSI3, ordenado por título, paginado."""
    nombre = unquote(nombre).strip()
    if not 1 <= len(nombre) <= 50:
        raise ErrorApi(400, "Nombre de categoría inválido")
    categoria = tabla().get_item(Key=clave_categoria(nombre)).get("Item")
    if not categoria or int(categoria.get("libros", 0)) <= 0:
        raise ErrorApi(404, f"Categoría '{nombre}' no encontrada")

    parametros: dict[str, Any] = {
        "IndexName": "GSI3",
        "KeyConditionExpression": Key("GSI3PK").eq(f"CATEGORIA#{normalizar(nombre)}"),
    }
    resultado = tabla().query(**paginar(parametros, query))
    libros = [libro_publico(i) for i in resultado.get("Items", [])]
    return respuesta(200, {
        "categoria": publico(categoria)["nombre"],
        "total": len(libros),
        "items": libros,
        "siguiente": codificar_cursor(resultado.get("LastEvaluatedKey")),
        "_fuente": "dynamodb",
    })
