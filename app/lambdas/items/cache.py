"""
cache.py
────────
Abstracción del cliente Redis para la Lambda /items (catálogo de libros).

Encapsula las operaciones de caché con manejo de errores, de modo que si
Redis no está disponible la Lambda sigue funcionando solo con DynamoDB
(degraded mode). DynamoDB es la fuente de verdad; Redis es la velocidad.

Estructuras de Redis usadas:
  - String     item#<id>                  → JSON del libro, con TTL (SETEX)
  - String     usuario#<id>               → JSON del usuario, con TTL
  - String     usuario#<id>#prestamos     → préstamos de un usuario, con TTL
  - Sorted Set ranking:items:consultas    → consultas por libro (ZINCRBY)
  - Sorted Set ranking:items:prestamos    → préstamos por libro (ZINCRBY)
  - String     usuario#<id>#multas        → multas de un usuario, con TTL
  - String     busqueda:<texto>           → resultados de GET /items/buscar, con TTL
  - Set        indice:busquedas           → claves busqueda:* vigentes (para invalidarlas)
  - Set        categorias                 → nombres de categorías, con TTL
  - List       cola:item#<id>             → cola de reservas de un libro (RPUSH / LREM)
  - Hash       stats:cache                → contadores hits / misses (HINCRBY)
"""
import json
import logging
import os
from typing import Any, cast

import redis

logger: logging.Logger = logging.getLogger(__name__)

# Sorted Sets con los rankings de libros
CLAVE_RANKING: str = "ranking:items:consultas"
CLAVE_RANKING_PRESTAMOS: str = "ranking:items:prestamos"
RANKINGS: dict[str, str] = {"consultas": CLAVE_RANKING, "prestamos": CLAVE_RANKING_PRESTAMOS}
# Hash con los contadores de aciertos / fallos del caché
CLAVE_STATS: str = "stats:cache"
# Prefijos de claves que se reportan en /cache/stats
PREFIJOS: tuple[str, ...] = ("item#", "usuario#", "busqueda:", "cola:")
CLAVE_INDICE_BUSQUEDAS: str = "indice:busquedas"
CLAVE_CATEGORIAS: str = "categorias"

# Cliente reutilizado entre invocaciones de la misma Lambda (contenedor caliente)
_cliente: redis.Redis | None = None


def _get_client() -> redis.Redis:
    """Crea (una sola vez) y retorna un cliente Redis."""
    global _cliente
    if _cliente is None:
        _cliente = redis.Redis(
            host=os.environ.get("REDIS_HOST", "redis"),
            port=int(os.environ.get("REDIS_PORT", 6379)),
            decode_responses=True,       # retorna str en lugar de bytes
            socket_connect_timeout=2,    # no bloquear la Lambda si Redis cae
            socket_timeout=2,
        )
    return _cliente


def ttl_por_defecto() -> int:
    """TTL de cada ítem en caché (segundos). Por defecto 300 = 5 minutos."""
    return int(os.environ.get("REDIS_TTL", 300))


def cache_key_item(item_id: str) -> str:
    """Convención de nombres de claves para ítems (libros)."""
    return f"item#{item_id}"


def cache_key_usuario(usuario_id: str) -> str:
    return f"usuario#{usuario_id}"


def cache_key_prestamos_usuario(usuario_id: str) -> str:
    return f"usuario#{usuario_id}#prestamos"


def cache_key_multas_usuario(usuario_id: str) -> str:
    return f"usuario#{usuario_id}#multas"


def cache_key_busqueda(texto_normalizado: str) -> str:
    return f"busqueda:{texto_normalizado}"


def cache_key_cola(item_id: str) -> str:
    return f"cola:item#{item_id}"


def _contar(campo: str) -> None:
    """HINCRBY stats:cache <hits|misses> 1 (no afecta la respuesta si falla)."""
    try:
        _get_client().hincrby(CLAVE_STATS, campo, 1)
    except redis.RedisError:
        pass


def cache_get(key: str) -> Any:
    """
    Paso 1 de Cache-Aside: r.get(key).
    Retorna el objeto deserializado, o None si no existe / hay error.
    """
    try:
        valor: str | None = cast(str | None, _get_client().get(key))
        if valor:
            datos: Any = json.loads(valor)
            logger.info("CACHE HIT  key=%s", key)
            _contar("hits")
            return datos
        logger.info("CACHE MISS key=%s", key)
        _contar("misses")
        return None
    except (redis.RedisError, json.JSONDecodeError) as e:
        logger.warning("Redis no disponible (get): %s", e)
        return None


def cache_set(key: str, valor: Any, ttl: int | None = None) -> bool:
    """
    Guarda un objeto en Redis con TTL por ítem: r.setex(key, ttl, json).
    SETEX equivale a SET + EXPIRE en una sola operación atómica.
    Retorna True si se guardó correctamente.
    """
    if ttl is None:
        ttl = ttl_por_defecto()
    try:
        _get_client().setex(key, ttl, json.dumps(valor, default=str))
        logger.info("CACHE SET  key=%s ttl=%ss", key, ttl)
        return True
    except redis.RedisError as e:
        logger.warning("Redis no disponible (set): %s", e)
        return False


def cache_delete(*keys: str) -> bool:
    """
    Invalida una o varias claves en Redis (DEL k1 k2 ...).
    Retorna True si la operación se ejecutó (existan o no las claves).
    """
    try:
        _get_client().delete(*keys)
        logger.info("CACHE DEL  keys=%s", ",".join(keys))
        return True
    except redis.RedisError as e:
        logger.warning("Redis no disponible (delete): %s", e)
        return False


# ── Ranking de consultas (Sorted Set) ─────────────────────────────────

def ranking_incrementar(item_id: str, clave: str = CLAVE_RANKING) -> None:
    """ZINCRBY <ranking> 1 <id> — cuenta una consulta o un préstamo."""
    try:
        _get_client().zincrby(clave, 1, item_id)
    except redis.RedisError as e:
        logger.warning("Redis no disponible (zincrby): %s", e)


def ranking_eliminar(item_id: str) -> None:
    """ZREM en ambos rankings — al borrar el libro."""
    try:
        for clave in RANKINGS.values():
            _get_client().zrem(clave, item_id)
    except redis.RedisError as e:
        logger.warning("Redis no disponible (zrem): %s", e)


def ranking_top(n: int, por: str = "consultas") -> list[dict[str, Any]] | None:
    """
    ZREVRANGE <ranking> 0 n-1 WITHSCORES  (por = "consultas" | "prestamos").
    Retorna [{"id": ..., "<por>": ...}] o None si Redis no responde.
    """
    try:
        filas = cast(
            list[tuple[str, float]],
            _get_client().zrevrange(RANKINGS[por], 0, n - 1, withscores=True),
        )
        return [{"id": item_id, por: int(score)} for item_id, score in filas]
    except redis.RedisError as e:
        logger.warning("Redis no disponible (zrevrange): %s", e)
        return None


# ── Búsquedas (String + Set índice) ───────────────────────────────────

def busqueda_set(key: str, resultados: Any) -> None:
    """Guarda una búsqueda con TTL y registra su clave en el Set índice."""
    if cache_set(key, resultados):
        try:
            cliente = _get_client()
            cliente.sadd(CLAVE_INDICE_BUSQUEDAS, key)
            cliente.expire(CLAVE_INDICE_BUSQUEDAS, ttl_por_defecto())
        except redis.RedisError as e:
            logger.warning("Redis no disponible (sadd): %s", e)


def invalidar_busquedas() -> None:
    """
    Al crear, editar o borrar un libro cambian los resultados de búsqueda:
    SMEMBERS indice:busquedas → DEL de todas esas claves y del índice.
    (Evita KEYS busqueda:*, que bloquea Redis.)
    """
    try:
        cliente = _get_client()
        claves = cast(set[str], cliente.smembers(CLAVE_INDICE_BUSQUEDAS))
        cliente.delete(CLAVE_INDICE_BUSQUEDAS, *claves)
        logger.info("CACHE DEL  %d búsquedas", len(claves))
    except redis.RedisError as e:
        logger.warning("Redis no disponible (invalidar búsquedas): %s", e)


# ── Categorías (Set) ──────────────────────────────────────────────────

def categorias_get() -> list[str] | None:
    """SMEMBERS categorias. None si no está en caché o Redis no responde."""
    try:
        miembros = cast(set[str], _get_client().smembers(CLAVE_CATEGORIAS))
    except redis.RedisError as e:
        logger.warning("Redis no disponible (smembers): %s", e)
        return None
    if miembros:
        _contar("hits")
        return sorted(miembros)
    _contar("misses")
    return None


def categorias_set(nombres: list[str]) -> None:
    """SADD categorias n1 n2 … + EXPIRE (TTL por clave)."""
    if not nombres:
        return
    try:
        cliente = _get_client()
        cliente.sadd(CLAVE_CATEGORIAS, *nombres)
        cliente.expire(CLAVE_CATEGORIAS, ttl_por_defecto())
    except redis.RedisError as e:
        logger.warning("Redis no disponible (sadd): %s", e)


# ── Cola de reservas (List) ───────────────────────────────────────────

def cola_leer(item_id: str) -> list[str] | None:
    """LRANGE cola:item#<id> 0 -1. None si Redis no responde."""
    try:
        return cast(list[str], _get_client().lrange(cache_key_cola(item_id), 0, -1))
    except redis.RedisError as e:
        logger.warning("Redis no disponible (lrange): %s", e)
        return None


def cola_reemplazar(item_id: str, reservas: list[str]) -> None:
    """Reconstruye la cola desde DynamoDB: DEL + RPUSH en una transacción MULTI."""
    try:
        tuberia = _get_client().pipeline()
        tuberia.delete(cache_key_cola(item_id))
        if reservas:
            tuberia.rpush(cache_key_cola(item_id), *reservas)
        tuberia.execute()
    except redis.RedisError as e:
        logger.warning("Redis no disponible (rpush): %s", e)


def cola_agregar(item_id: str, reserva_id: str) -> None:
    """RPUSH cola:item#<id> <reservaId> — entra al final de la fila."""
    try:
        _get_client().rpush(cache_key_cola(item_id), reserva_id)
    except redis.RedisError as e:
        logger.warning("Redis no disponible (rpush): %s", e)


def cola_quitar(item_id: str, reserva_id: str) -> None:
    """LREM cola:item#<id> 0 <reservaId> — sale de la fila (cancelada o atendida)."""
    try:
        _get_client().lrem(cache_key_cola(item_id), 0, reserva_id)
    except redis.RedisError as e:
        logger.warning("Redis no disponible (lrem): %s", e)


# ── Estadísticas del caché ────────────────────────────────────────────

def cache_stats() -> dict[str, Any] | None:
    """
    Resumen del caché: HGETALL stats:cache, DBSIZE y cantidad de claves por
    prefijo (SCAN, no KEYS, para no bloquear Redis). None si Redis no responde.
    """
    try:
        cliente = _get_client()
        contadores = cast(dict[str, str], cliente.hgetall(CLAVE_STATS))
        hits = int(contadores.get("hits", 0))
        misses = int(contadores.get("misses", 0))
        total = hits + misses
        claves = {
            prefijo.rstrip("#:"): sum(1 for _ in cliente.scan_iter(match=f"{prefijo}*", count=100))
            for prefijo in PREFIJOS
        }
        return {
            "hits": hits,
            "misses": misses,
            "hit_ratio": round(hits / total, 3) if total else None,
            "claves_totales": int(cast(int, cliente.dbsize())),
            "claves_por_tipo": claves,
            "ttl_segundos": ttl_por_defecto(),
        }
    except redis.RedisError as e:
        logger.warning("Redis no disponible (stats): %s", e)
        return None
