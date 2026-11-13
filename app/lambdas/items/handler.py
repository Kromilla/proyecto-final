"""
Lambda del catálogo de la biblioteca (ItemsHandler)
───────────────────────────────────────────────────
Una sola Lambda atiende todas las rutas del dominio para compartir el
cliente de Redis (cache.py) y el de DynamoDB (comun.py) entre módulos.

El enrutamiento usa el RECURSO de API Gateway que llega en el evento
("/items/{id}", "/items/buscar", ...), no la URL. API Gateway ya resolvió
que /items/buscar es el recurso fijo "buscar" y no el {id} "buscar", así que
el orden en que se declaran las rutas no importa: es un diccionario.

  Libros      POST /items · GET /items · GET /items/buscar · GET /items/populares
              GET /items/{id} · GET /items/{id}/reservas · PUT /items/{id} · DELETE /items/{id}
  Categorías  GET /categorias · GET /categorias/{nombre}/items
  Usuarios    POST /usuarios · GET /usuarios · GET /usuarios/{id} · PUT /usuarios/{id}
              DELETE /usuarios/{id} · GET /usuarios/{id}/prestamos · GET /usuarios/{id}/multas
  Préstamos   POST /prestamos · GET /prestamos · GET /prestamos/vencidos · GET /prestamos/{id}
              PUT /prestamos/{id}/devolver · PUT /prestamos/{id}/renovar
  Reservas    POST /reservas · GET /items/{id}/reservas · DELETE /reservas/{id}
  Multas      POST /multas · POST /multas/{id}/pagar
  Caché       GET /cache/stats
  (GET /health vive en su propia Lambda: lambdas/health)

Single Table Design (una tabla, claves PK/SK + GSI1 + GSI2 + GSI3):
  Libro      PK=ITEM#<id>          SK=METADATA
  Categoría  PK=CATEGORIA#<nombre> SK=METADATA
  Usuario    PK=USER#<id>          SK=PROFILE
  Préstamo   PK=USER#<id>          SK=LOAN#<fecha>#<itemId>#<prestamoId>
  Reserva    PK=USER#<id>          SK=RESV#<itemId>
  Multa      PK=USER#<id>          SK=FINE#<fecha>#<multaId>
"""
import logging
from typing import Any, Callable

from botocore.exceptions import ClientError

import cache
import categorias
import libros
import multas
import prestamos
import reservas
import usuarios
from comun import PATRON_ID, ErrorApi, RespuestaApi, error, respuesta

logger: logging.Logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Una ruta recibe (parámetro de ruta, evento, query string)
Ruta = Callable[[str | None, dict[str, Any], dict[str, str]], RespuestaApi]


def _cache_stats() -> RespuestaApi:
    stats = cache.cache_stats()
    if stats is None:
        return error(503, "Redis no responde")
    return respuesta(200, stats)


# (método, recurso de API Gateway) → función
RUTAS: dict[tuple[str, str], Ruta] = {
    # Libros
    ("POST",   "/items"):                     lambda _p, ev, _q: libros.crear(ev),
    ("GET",    "/items"):                     lambda _p, _e, q: libros.listar(q),
    ("GET",    "/items/buscar"):              lambda _p, _e, q: libros.buscar(q),
    ("GET",    "/items/populares"):           lambda _p, _e, q: libros.populares(q),
    ("GET",    "/items/{id}"):                lambda p, _e, _q: libros.obtener(p),
    ("GET",    "/items/{id}/reservas"):       lambda p, _e, _q: reservas.listar_de_item(p),
    ("PUT",    "/items/{id}"):                lambda p, ev, _q: libros.actualizar(p, ev),
    ("DELETE", "/items/{id}"):                lambda p, _e, _q: libros.eliminar(p),
    # Categorías
    ("GET",    "/categorias"):                lambda _p, _e, _q: categorias.listar(),
    ("GET",    "/categorias/{nombre}/items"): lambda p, _e, q: categorias.libros_de(p, q),
    # Usuarios
    ("POST",   "/usuarios"):                  lambda _p, ev, _q: usuarios.crear(ev),
    ("GET",    "/usuarios"):                  lambda _p, _e, q: usuarios.listar(q),
    ("GET",    "/usuarios/{id}"):             lambda p, _e, _q: usuarios.obtener(p),
    ("PUT",    "/usuarios/{id}"):             lambda p, ev, _q: usuarios.actualizar(p, ev),
    ("DELETE", "/usuarios/{id}"):             lambda p, _e, _q: usuarios.eliminar(p),
    ("GET",    "/usuarios/{id}/prestamos"):   lambda p, _e, q: usuarios.listar_prestamos(p, q),
    ("GET",    "/usuarios/{id}/multas"):      lambda p, _e, _q: multas.listar(p),
    # Préstamos
    ("POST",   "/prestamos"):                 lambda _p, ev, _q: prestamos.crear(ev),
    ("GET",    "/prestamos"):                 lambda _p, _e, q: prestamos.listar(q),
    ("GET",    "/prestamos/vencidos"):        lambda _p, _e, q: prestamos.vencidos(q),
    ("GET",    "/prestamos/{id}"):            lambda p, _e, _q: prestamos.obtener(p),
    ("PUT",    "/prestamos/{id}/devolver"):   lambda p, _e, _q: prestamos.devolver(p),
    ("PUT",    "/prestamos/{id}/renovar"):    lambda p, _e, _q: prestamos.renovar(p),
    # Reservas
    ("POST",   "/reservas"):                  lambda _p, ev, _q: reservas.crear(ev),
    ("DELETE", "/reservas/{id}"):             lambda p, _e, _q: reservas.cancelar(p),
    # Multas
    ("POST",   "/multas"):                    lambda _p, ev, _q: multas.crear(ev),
    ("POST",   "/multas/{id}/pagar"):         lambda p, _e, _q: multas.pagar(p),
    # Caché
    ("GET",    "/cache/stats"):               lambda _p, _e, _q: _cache_stats(),
}
RECURSOS: frozenset[str] = frozenset(recurso for _, recurso in RUTAS)


def main(event: dict[str, Any], context: Any) -> RespuestaApi:
    """Enruta por (método, recurso). ErrorApi usa su status; un ClientError de DynamoDB responde 500."""
    metodo: str = event.get("httpMethod", "GET")
    recurso: str = event.get("resource") or event.get("path") or ""
    parametros: dict[str, str] = event.get("pathParameters") or {}
    query: dict[str, str] = event.get("queryStringParameters") or {}
    valor: str | None = parametros.get("id", parametros.get("nombre"))
    logger.info("REQUEST %s %s param=%s", metodo, recurso, valor)

    ruta = RUTAS.get((metodo, recurso))
    if ruta is None:
        if recurso in RECURSOS:
            return error(405, f"Método '{metodo}' no soportado en {recurso}")
        return error(404, f"Ruta '{recurso}' no encontrada")
    if "{id}" in recurso and (valor is None or not PATRON_ID.match(valor)):
        return error(400, "Id inválido")
    if "{nombre}" in recurso and not valor:
        return error(400, "Falta el nombre")

    try:
        return ruta(valor, event, query)
    except ErrorApi as e:
        return error(e.status, e.mensaje)
    except ClientError as e:
        logger.error("Error DynamoDB: %s", e)
        return error(500, "Error al acceder a la base de datos")
