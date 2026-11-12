"""
test_cache.py
─────────────
Tests unitarios de cache.py: get / set / delete, TTL por ítem, ranking
con Sorted Set y comportamiento degradado cuando Redis no está disponible.
"""
import pytest
import redis

import cache


@pytest.fixture(autouse=True)
def _redis(parchear_redis):
    return parchear_redis


class RedisCaido:
    def __getattr__(self, _nombre):
        def falla(*_a, **_k):
            raise redis.ConnectionError("sin conexión")
        return falla


# ── cache_set / cache_get ─────────────────────────────────────────────

def test_set_y_get_basico():
    item = {"id": "x1", "titulo": "Test"}
    assert cache.cache_set("item#x1", item, ttl=60) is True
    assert cache.cache_get("item#x1") == item


def test_get_clave_inexistente_retorna_none():
    assert cache.cache_get("item#no-existe") is None


def test_set_respeta_ttl(redis_fake):
    cache.cache_set("item#ttl", {"v": 1}, ttl=10)
    assert 1 <= redis_fake.ttl("item#ttl") <= 10


def test_ttl_por_defecto_es_300(monkeypatch, redis_fake):
    monkeypatch.delenv("REDIS_TTL", raising=False)
    cache.cache_set("item#def", {"v": 1})
    assert 290 <= redis_fake.ttl("item#def") <= 300


def test_set_usa_ttl_de_entorno(monkeypatch, redis_fake):
    monkeypatch.setenv("REDIS_TTL", "120")
    cache.cache_set("item#env", {"v": 2})
    assert 100 <= redis_fake.ttl("item#env") <= 120


def test_get_valor_corrupto_retorna_none(redis_fake):
    redis_fake.set("item#roto", "{no es json")
    assert cache.cache_get("item#roto") is None


# ── cache_delete ──────────────────────────────────────────────────────

def test_delete_elimina_clave():
    cache.cache_set("item#del", {"v": 3}, ttl=60)
    assert cache.cache_get("item#del") is not None
    cache.cache_delete("item#del")
    assert cache.cache_get("item#del") is None


def test_delete_clave_inexistente_no_falla():
    assert cache.cache_delete("item#no-existe") is True


# ── Ranking (Sorted Set) ──────────────────────────────────────────────

def test_ranking_incrementa_y_ordena(redis_fake):
    for item_id in ["a", "b", "b", "c", "c", "c"]:
        cache.ranking_incrementar(item_id)
    assert cache.ranking_top(2) == [{"id": "c", "consultas": 3}, {"id": "b", "consultas": 2}]
    assert redis_fake.zscore(cache.CLAVE_RANKING, "a") == 1


def test_ranking_eliminar(redis_fake):
    cache.ranking_incrementar("a")
    cache.ranking_eliminar("a")
    assert cache.ranking_top(10) == []


# ── Modo degradado (Redis no disponible) ──────────────────────────────

def test_operaciones_con_redis_caido_no_lanzan_excepcion(monkeypatch):
    monkeypatch.setattr(cache, "_get_client", lambda: RedisCaido())
    assert cache.cache_get("item#x") is None
    assert cache.cache_set("item#x", {"v": 1}) is False
    assert cache.cache_delete("item#x") is False
    cache.ranking_incrementar("x")
    cache.ranking_eliminar("x")
    assert cache.ranking_top(5) is None


# ── Convención de nombres ─────────────────────────────────────────────

def test_cache_key_item_formato():
    assert cache.cache_key_item("abc-123") == "item#abc-123"


def test_cliente_real_se_reutiliza(monkeypatch):
    """_get_client crea un solo cliente por contenedor Lambda (sin conectar aún)."""
    monkeypatch.undo()
    monkeypatch.setattr(cache, "_cliente", None)
    primero = cache._get_client()
    assert primero is cache._get_client()
    assert isinstance(primero, redis.Redis)


# ── Varias claves, ranking de préstamos y estadísticas ───────────────

def test_delete_varias_claves(redis_fake):
    cache.cache_set("item#a", {"v": 1})
    cache.cache_set("usuario#b", {"v": 2})
    cache.cache_delete("item#a", "usuario#b")
    assert redis_fake.dbsize() == 0


def test_set_y_get_de_listas():
    cache.cache_set("usuario#x#prestamos", [{"id": "p1"}])
    assert cache.cache_get("usuario#x#prestamos") == [{"id": "p1"}]


def test_ranking_por_prestamos():
    cache.ranking_incrementar("a", cache.CLAVE_RANKING_PRESTAMOS)
    assert cache.ranking_top(5, "prestamos") == [{"id": "a", "prestamos": 1}]
    assert cache.ranking_top(5, "consultas") == []


def test_stats_cuenta_hits_misses_y_claves():
    cache.cache_set("item#a", {"v": 1})
    cache.cache_set("usuario#b", {"v": 2})
    cache.cache_get("item#a")          # hit
    cache.cache_get("item#a")          # hit
    cache.cache_get("item#zzz")        # miss
    stats = cache.cache_stats()
    assert stats["hits"] == 2 and stats["misses"] == 1
    assert stats["hit_ratio"] == 0.667
    assert stats["claves_por_tipo"] == {"item": 1, "usuario": 1, "busqueda": 0, "cola": 0}
    assert stats["ttl_segundos"] == 300


def test_stats_sin_actividad():
    stats = cache.cache_stats()
    assert stats["hits"] == 0 and stats["hit_ratio"] is None


def test_stats_con_redis_caido(monkeypatch):
    monkeypatch.setattr(cache, "_get_client", lambda: RedisCaido())
    assert cache.cache_stats() is None


# ── Endpoint GET /cache/stats ─────────────────────────────────────────

def test_endpoint_cache_stats(api):
    api("POST", "/items", body={"id": "l1", "titulo": "T", "autor": "A"})
    api("GET", "/items/{id}", id="l1")       # hit (Write-Through)
    api("GET", "/items/{id}", id="nope")     # miss
    status, cuerpo = api("GET", "/cache/stats")
    assert status == 200
    assert cuerpo["hits"] == 1 and cuerpo["misses"] == 1
    assert cuerpo["claves_por_tipo"]["item"] == 1


def test_endpoint_cache_stats_redis_caido(api, monkeypatch):
    monkeypatch.setattr(cache, "_get_client", lambda: RedisCaido())
    assert api("GET", "/cache/stats")[0] == 503
