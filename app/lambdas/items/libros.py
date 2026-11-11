"""
libros.py — Libros del catálogo (rutas /items)
──────────────────────────────────────────────
  POST   /items             Crear libro          Write-Through (DynamoDB + Redis)
  GET    /items             Listar libros        Query GSI2 (o GSI1 con ?autor=)
  GET    /items/buscar      Buscar por título    Cache-Aside de resultados (String + Set índice)
  GET    /items/populares   Ranking              Sorted Set de Redis
  GET    /items/{id}        Obtener libro        Cache-Aside (Redis → DynamoDB)
  PUT    /items/{id}        Actualizar libro     Write-Through (sobrescribe la caché)
  DELETE /items/{id}        Eliminar libro       Invalida la clave en Redis

Ítem en la tabla:
  PK=ITEM#<id>  SK=METADATA
  GSI1PK=AUTOR#<autor>          GSI1SK=ANIO#<anio>#ITEM#<id>   (libros por autor)
  GSI2PK=TIPO#LIBRO             GSI2SK=TITULO#<titulo>#<id>     (catálogo por título)
  GSI3PK=CATEGORIA#<categoria>  GSI3SK=TITULO#<titulo>#<id>     (libros por categoría)

Cada alta, edición o baja actualiza en la MISMA transacción el contador de
libros de su categoría (PK=CATEGORIA#<nombre>, SK=METADATA, ver categorias.py).
"""
import logging
from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

import cache
import comun
from comun import (
    TABLE_NAME, ErrorApi, Item, RespuestaApi, ahora_iso, codificar_cursor, entero,
    error, es_cancelada, leer_json, normalizar, nuevo_id, paginar, publico,
    query_completa, respuesta, tabla, texto, validar_id,
)

logger: logging.Logger = logging.getLogger(__name__)

CAMPOS_TEXTO: tuple[str, ...] = (
    "titulo", "autor", "categoria", "isbn", "genero", "editorial", "descripcion")
CAMPOS_EDITABLES: frozenset[str] = frozenset(CAMPOS_TEXTO) | {"anio", "ejemplares"}
CAMPOS_BUSQUEDA: tuple[str, ...] = ("id", "titulo", "autor", "anio", "categoria")
MAX_EJEMPLARES: int = 1000
MAX_RESULTADOS_BUSQUEDA: int = 50


def clave_libro(item_id: str) -> dict[str, str]:
    return {"PK": f"ITEM#{item_id}", "SK": "METADATA"}


def clave_categoria(nombre: str) -> dict[str, str]:
    return {"PK": f"CATEGORIA#{normalizar(nombre)}", "SK": "METADATA"}


def libro_publico(item: Item) -> Item:
    datos = publico(item)
    datos["disponible"] = datos.get("disponibles", 0) > 0
    return datos


def _validar_campos(datos: dict[str, Any], parcial: bool) -> Item:
    """Valida los campos de un libro (todos en POST, solo los enviados en PUT)."""
    libro: Item = {}
    for campo in CAMPOS_TEXTO:
        obligatorio = (campo in ("titulo", "autor") and (not parcial or campo in datos)) \
            or (campo == "categoria" and campo in datos)
        valor = texto(datos, campo, obligatorio=obligatorio)
        if valor is not None:
            libro[campo] = valor
    if "categoria" in libro and len(libro["categoria"]) > 50:
        raise ErrorApi(400, "El campo 'categoria' admite máximo 50 caracteres")
    if "anio" in datos:
        libro["anio"] = entero(datos["anio"], "anio", 1, comun.hoy().year + 1)
    if "ejemplares" in datos:
        libro["ejemplares"] = entero(datos["ejemplares"], "ejemplares", 0, MAX_EJEMPLARES)
    return libro


def _claves_indices(libro: Item) -> Item:
    """Claves de GSI1, GSI2 y GSI3 calculadas a partir de los datos del libro."""
    item_id = libro["id"]
    titulo = normalizar(libro["titulo"])
    claves: Item = {
        "GSI1PK": f"AUTOR#{normalizar(libro['autor'])}",
        "GSI1SK": f"ANIO#{libro.get('anio', 0):04d}#ITEM#{item_id}",
        "GSI2PK": "TIPO#LIBRO",
        "GSI2SK": f"TITULO#{titulo}#{item_id}",
        "titulo_busqueda": titulo,
    }
    if libro.get("categoria"):
        claves["GSI3PK"] = f"CATEGORIA#{normalizar(libro['categoria'])}"
        claves["GSI3SK"] = f"TITULO#{titulo}#{item_id}"
    return claves


def _op_categoria(nombre: str, delta: int) -> dict[str, Any]:
    """Operación de transacción: suma `delta` al contador de la categoría."""
    return {"Update": {
        "TableName": TABLE_NAME,
        "Key": clave_categoria(nombre),
        "UpdateExpression": ("SET nombre = if_not_exists(nombre, :nombre), tipo = :tipo, "
                             "GSI2PK = :gsi2pk, GSI2SK = :gsi2sk "
                             "ADD libros :delta"),
        "ExpressionAttributeValues": {
            ":nombre": nombre, ":tipo": "CATEGORIA", ":gsi2pk": "TIPO#CATEGORIA",
            ":gsi2sk": f"CATEGORIA#{normalizar(nombre)}", ":delta": delta,
        },
    }}


def _invalidar_listados(*categorias: str | None) -> None:
    """Un cambio en el catálogo vuelve viejas las búsquedas y el Set de categorías."""
    cache.invalidar_busquedas()
    if any(categorias):
        cache.cache_delete(cache.CLAVE_CATEGORIAS)


def obtener_de_dynamodb(item_id: str, consistente: bool = False) -> Item | None:
    return tabla().get_item(Key=clave_libro(item_id), ConsistentRead=consistente).get("Item")


# ── Handlers ──────────────────────────────────────────────────────────

def crear(event: dict[str, Any]) -> RespuestaApi:
    """POST /items. Crea el libro y suma el contador de su categoría. 409 si el id existe."""
    datos = leer_json(event)
    libro = _validar_campos(datos, parcial=False)
    libro["id"] = validar_id(datos["id"]) if "id" in datos else nuevo_id()
    libro.setdefault("ejemplares", 1)
    momento = ahora_iso()
    libro.update(disponibles=libro["ejemplares"], prestados=0, reservas_pendientes=0,
                 creado_en=momento, actualizado_en=momento)
    item: Item = {**clave_libro(libro["id"]), **_claves_indices(libro), "tipo": "LIBRO", **libro}

    operaciones: list[dict[str, Any]] = [{"Put": {
        "TableName": TABLE_NAME, "Item": item,
        "ConditionExpression": "attribute_not_exists(PK)",
    }}]
    if libro.get("categoria"):
        operaciones.append(_op_categoria(libro["categoria"], 1))
    try:
        comun.cliente().transact_write_items(TransactItems=operaciones)
    except ClientError as e:
        if es_cancelada(e):
            raise ErrorApi(409, f"Ya existe un ítem con id '{libro['id']}'")
        raise
    logger.info("DynamoDB PUT libro=%s", libro["id"])

    salida = libro_publico(item)
    cache.cache_set(cache.cache_key_item(libro["id"]), salida)   # Write-Through
    _invalidar_listados(libro.get("categoria"))
    return respuesta(201, salida)


def listar(query: dict[str, str]) -> RespuestaApi:
    """GET /items — catálogo por título (GSI2) o por autor (GSI1). Sin Scan."""
    if query.get("autor"):
        parametros = {"IndexName": "GSI1",
                      "KeyConditionExpression": Key("GSI1PK").eq(f"AUTOR#{normalizar(query['autor'])}")}
    else:
        parametros = {"IndexName": "GSI2",
                      "KeyConditionExpression": Key("GSI2PK").eq("TIPO#LIBRO")}
    resultado = tabla().query(**paginar(parametros, query))
    libros = [libro_publico(i) for i in resultado.get("Items", [])]
    return respuesta(200, {
        "items": libros,
        "total": len(libros),
        "siguiente": codificar_cursor(resultado.get("LastEvaluatedKey")),
        "_fuente": "dynamodb",
    })


def buscar(query: dict[str, str]) -> RespuestaApi:
    """
    GET /items/buscar?q=<texto> — título que CONTIENE el texto, sin importar
    mayúsculas ni tildes. Cache-Aside sobre busqueda:<texto normalizado>.
    DynamoDB no es un motor de texto completo: se hace Query sobre la
    partición TIPO#LIBRO del GSI2 con FilterExpression contains(). El caché
    evita repetir esa lectura para las búsquedas frecuentes.
    """
    crudo = (query.get("q") or "").strip()
    if not 2 <= len(crudo) <= 100:
        raise ErrorApi(400, "'q' debe tener entre 2 y 100 caracteres")
    q = normalizar(crudo)
    clave = cache.cache_key_busqueda(q)

    cacheado = cache.cache_get(clave)
    if cacheado is not None:
        return respuesta(200, {**cacheado, "_fuente": "cache"})

    encontrados = query_completa(
        IndexName="GSI2",
        KeyConditionExpression=Key("GSI2PK").eq("TIPO#LIBRO"),
        FilterExpression=Attr("titulo_busqueda").contains(q),
    )[:MAX_RESULTADOS_BUSQUEDA]
    resultado = {
        "q": crudo,
        "total": len(encontrados),
        "items": [{c: v for c, v in publico(i).items() if c in CAMPOS_BUSQUEDA}
                  for i in encontrados],
    }
    cache.busqueda_set(clave, resultado)
    return respuesta(200, {**resultado, "_fuente": "dynamodb"})


def populares(query: dict[str, str]) -> RespuestaApi:
    """GET /items/populares?por=consultas|prestamos&n=5 — Sorted Set de Redis."""
    por = query.get("por", "consultas")
    if por not in cache.RANKINGS:
        raise ErrorApi(400, "'por' debe ser 'consultas' o 'prestamos'")
    n = comun.entero_query(query, "n", 5, comun.MAX_LISTA)
    ranking = cache.ranking_top(n, por)
    if ranking is None:
        return error(503, "El ranking no está disponible (Redis no responde)")
    return respuesta(200, {"por": por, "items": ranking, "_fuente": "cache"})


def obtener(item_id: str) -> RespuestaApi:
    """
    GET /items/{id} — Cache-Aside:
      1. r.get(key)        → hit: "_fuente": "cache"
      2. table.get_item()  → miss: lee DynamoDB
      3. r.setex(key, 300) → guarda para las siguientes consultas
    """
    clave = cache.cache_key_item(item_id)
    cacheado = cache.cache_get(clave)
    if cacheado:
        cache.ranking_incrementar(item_id)
        return respuesta(200, {**cacheado, "_fuente": "cache"})

    item = obtener_de_dynamodb(item_id)
    if not item:
        raise ErrorApi(404, f"Ítem '{item_id}' no encontrado")
    salida = libro_publico(item)
    cache.cache_set(clave, salida)
    cache.ranking_incrementar(item_id)
    return respuesta(200, {**salida, "_fuente": "dynamodb"})


def actualizar(item_id: str, event: dict[str, Any]) -> RespuestaApi:
    """
    PUT /items/{id} — actualización parcial con UpdateItem (no pisa los
    contadores prestados / reservas_pendientes) + Write-Through.
    """
    datos = leer_json(event)
    desconocidos = set(datos) - CAMPOS_EDITABLES
    if desconocidos:
        raise ErrorApi(400, f"Campos no editables: {', '.join(sorted(desconocidos))}")
    if not datos:
        raise ErrorApi(400, "Envíe al menos un campo para actualizar")
    cambios = _validar_campos(datos, parcial=True)

    actual = obtener_de_dynamodb(item_id)
    if not actual:
        raise ErrorApi(404, f"Ítem '{item_id}' no encontrado")
    prestados = int(actual.get("prestados", 0))
    if "ejemplares" in cambios and cambios["ejemplares"] < prestados:
        raise ErrorApi(409, f"No se puede dejar {cambios['ejemplares']} ejemplares: "
                            f"hay {prestados} prestados")

    nuevo: Item = {**publico(actual), **cambios}
    valores: dict[str, Any] = {**cambios, **_claves_indices(nuevo), "actualizado_en": ahora_iso()}
    nombres = {f"#c{i}": campo for i, campo in enumerate(valores)}
    asignaciones = [f"{n} = :v{i}" for i, n in enumerate(nombres)]
    expresion_valores = {f":v{i}": v for i, v in enumerate(valores.values())}
    condicion = "attribute_exists(PK)"
    if "ejemplares" in cambios:
        # disponibles se recalcula en DynamoDB con el valor actual de prestados;
        # la condición evita dejar menos ejemplares que préstamos (carrera)
        asignaciones.append("disponibles = :ej - prestados")
        expresion_valores[":ej"] = cambios["ejemplares"]
        condicion += " AND prestados <= :ej"

    operaciones: list[dict[str, Any]] = [{"Update": {
        "TableName": TABLE_NAME,
        "Key": clave_libro(item_id),
        "UpdateExpression": "SET " + ", ".join(asignaciones),
        "ConditionExpression": condicion,
        "ExpressionAttributeNames": nombres,
        "ExpressionAttributeValues": expresion_valores,
    }}]
    cat_vieja, cat_nueva = actual.get("categoria"), nuevo.get("categoria")
    if cat_nueva and normalizar(cat_nueva) != normalizar(cat_vieja or ""):
        operaciones.append(_op_categoria(cat_nueva, 1))
        if cat_vieja:
            operaciones.append(_op_categoria(cat_vieja, -1))
    try:
        comun.cliente().transact_write_items(TransactItems=operaciones)
    except ClientError as e:
        if es_cancelada(e):
            raise ErrorApi(409, "El libro cambió mientras se actualizaba; intente de nuevo")
        raise
    logger.info("DynamoDB UPDATE libro=%s", item_id)

    salida = libro_publico(obtener_de_dynamodb(item_id, consistente=True) or {})
    cache.cache_set(cache.cache_key_item(item_id), salida)   # Write-Through
    _invalidar_listados(cat_vieja, cat_nueva)
    return respuesta(200, salida)


def eliminar(item_id: str) -> RespuestaApi:
    """DELETE /items/{id} — sin préstamos ni reservas pendientes; invalida Redis."""
    actual = obtener_de_dynamodb(item_id)
    if not actual:
        raise ErrorApi(404, f"Ítem '{item_id}' no encontrado")
    if int(actual.get("prestados", 0)) > 0:
        raise ErrorApi(409, f"El ítem '{item_id}' tiene préstamos activos")
    if int(actual.get("reservas_pendientes", 0)) > 0:
        raise ErrorApi(409, f"El ítem '{item_id}' tiene reservas pendientes")

    operaciones: list[dict[str, Any]] = [{"Delete": {
        "TableName": TABLE_NAME,
        "Key": clave_libro(item_id),
        "ConditionExpression": ("attribute_exists(PK) AND prestados = :cero AND "
                                "(attribute_not_exists(reservas_pendientes) OR reservas_pendientes = :cero)"),
        "ExpressionAttributeValues": {":cero": 0},
    }}]
    if actual.get("categoria"):
        operaciones.append(_op_categoria(actual["categoria"], -1))
    try:
        comun.cliente().transact_write_items(TransactItems=operaciones)
    except ClientError as e:
        if es_cancelada(e):
            raise ErrorApi(409, f"El ítem '{item_id}' cambió mientras se eliminaba; intente de nuevo")
        raise
    logger.info("DynamoDB DELETE libro=%s", item_id)

    cache.cache_delete(cache.cache_key_item(item_id), cache.cache_key_cola(item_id))  # invalidación
    cache.ranking_eliminar(item_id)
    _invalidar_listados(actual.get("categoria"))
    return respuesta(200, {"mensaje": f"Ítem '{item_id}' eliminado"})
