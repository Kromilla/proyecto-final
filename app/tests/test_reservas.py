"""
test_reservas.py
────────────────
Tests de POST /reservas y DELETE /reservas/{id}: registro en DynamoDB,
cola en una List de Redis (RPUSH / LREM) y su efecto en los préstamos.
"""
import pytest

import cache


@pytest.fixture
def agotado(api):
    """Libro con 1 ejemplar ya prestado a 'ana'; 'beto' y 'caro' esperan."""
    api("POST", "/items", body={"id": "cien", "titulo": "Cien años de soledad",
                                "autor": "GGM", "ejemplares": 1})
    for uid in ("ana", "beto", "caro"):
        api("POST", "/usuarios", body={"id": uid, "nombre": uid.title(), "email": f"{uid}@unimag.edu.co"})
    _, prestamo = api("POST", "/prestamos", body={"usuario_id": "ana", "item_id": "cien"})
    api.prestamo_ana = prestamo["id"]
    return api


def reservar(api, usuario, item="cien"):
    return api("POST", "/reservas", body={"usuario_id": usuario, "item_id": item})


# ── POST /reservas ────────────────────────────────────────────────────

def test_reservar_crea_registro_y_entra_a_la_cola(agotado, tabla_dynamodb, redis_fake):
    status, r1 = reservar(agotado, "beto")
    assert status == 201 and r1["posicion"] == 1 and r1["estado"] == "PENDIENTE"
    _, r2 = reservar(agotado, "caro")
    assert r2["posicion"] == 2
    # Redis List en orden de llegada (RPUSH)
    assert redis_fake.lrange(cache.cache_key_cola("cien"), 0, -1) == [r1["id"], r2["id"]]
    # DynamoDB: registro en la partición del usuario + contador en el libro
    item = tabla_dynamodb.get_item(Key={"PK": "USER#beto", "SK": "RESV#cien"})["Item"]
    assert item["GSI3PK"] == "RESERVAS#ITEM#cien"
    libro = tabla_dynamodb.get_item(Key={"PK": "ITEM#cien", "SK": "METADATA"})["Item"]
    assert libro["reservas_pendientes"] == 2


def test_reservar_invalida_cache_del_libro(agotado, redis_fake):
    agotado("GET", "/items/{id}", id="cien")
    reservar(agotado, "beto")
    _, libro = agotado("GET", "/items/{id}", id="cien")
    assert libro["_fuente"] == "dynamodb" and libro["reservas_pendientes"] == 1


def test_reservar_con_stock_retorna_409(api):
    api("POST", "/items", body={"id": "l1", "titulo": "T", "autor": "A"})
    api("POST", "/usuarios", body={"id": "ana", "nombre": "Ana", "email": "ana@unimag.edu.co"})
    status, cuerpo = reservar(api, "ana", "l1")
    assert status == 409 and "disponibles" in cuerpo["error"]


def test_reservar_dos_veces_retorna_409(agotado):
    reservar(agotado, "beto")
    assert reservar(agotado, "beto")[0] == 409


@pytest.mark.parametrize("usuario, item", [("nadie", "cien"), ("beto", "no-existe")])
def test_reservar_inexistente_404(agotado, usuario, item):
    assert reservar(agotado, usuario, item)[0] == 404


@pytest.mark.parametrize("body", [{"usuario_id": "beto"}, {"item_id": "cien"},
                                  {"usuario_id": "b b", "item_id": "cien"}])
def test_reservar_body_invalido_400(agotado, body):
    assert agotado("POST", "/reservas", body=body)[0] == 400


def test_cola_se_reconstruye_si_redis_la_pierde(agotado, redis_fake):
    _, r1 = reservar(agotado, "beto")
    redis_fake.delete(cache.cache_key_cola("cien"))       # Redis perdió la List
    _, r2 = reservar(agotado, "caro")
    assert r2["posicion"] == 2
    assert redis_fake.lrange(cache.cache_key_cola("cien"), 0, -1) == [r1["id"], r2["id"]]


# ── DELETE /reservas/{id} ─────────────────────────────────────────────

def test_cancelar_reserva_hace_lrem_y_descuenta(agotado, tabla_dynamodb, redis_fake):
    _, r1 = reservar(agotado, "beto")
    _, r2 = reservar(agotado, "caro")
    status, _ = agotado("DELETE", "/reservas/{id}", id=r1["id"])
    assert status == 200
    assert redis_fake.lrange(cache.cache_key_cola("cien"), 0, -1) == [r2["id"]]
    assert "Item" not in tabla_dynamodb.get_item(Key={"PK": "USER#beto", "SK": "RESV#cien"})
    libro = tabla_dynamodb.get_item(Key={"PK": "ITEM#cien", "SK": "METADATA"})["Item"]
    assert libro["reservas_pendientes"] == 1


def test_cancelar_dos_veces_o_inexistente_404(agotado):
    _, r1 = reservar(agotado, "beto")
    agotado("DELETE", "/reservas/{id}", id=r1["id"])
    assert agotado("DELETE", "/reservas/{id}", id=r1["id"])[0] == 404
    assert agotado("DELETE", "/reservas/{id}", id="nope")[0] == 404


# ── Efecto de la fila en los préstamos ────────────────────────────────

def test_al_devolver_solo_presta_el_primero_de_la_fila(agotado, tabla_dynamodb, redis_fake):
    _, r1 = reservar(agotado, "beto")
    reservar(agotado, "caro")
    _, devuelto = agotado("PUT", "/prestamos/{id}/devolver", id=agotado.prestamo_ana)
    assert devuelto["reservas_en_espera"] == 2

    # caro es segunda: no puede llevárselo
    status, cuerpo = agotado("POST", "/prestamos", body={"usuario_id": "caro", "item_id": "cien"})
    assert status == 409 and "reservado" in cuerpo["error"]
    # beto es primero: el préstamo consume su reserva
    assert agotado("POST", "/prestamos", body={"usuario_id": "beto", "item_id": "cien"})[0] == 201
    assert "Item" not in tabla_dynamodb.get_item(Key={"PK": "USER#beto", "SK": "RESV#cien"})
    assert r1["id"] not in redis_fake.lrange(cache.cache_key_cola("cien"), 0, -1)
    libro = tabla_dynamodb.get_item(Key={"PK": "ITEM#cien", "SK": "METADATA"})["Item"]
    assert libro["reservas_pendientes"] == 1 and libro["disponibles"] == 0


def test_libro_con_reservas_no_se_puede_borrar(agotado):
    reservar(agotado, "beto")
    agotado("PUT", "/prestamos/{id}/devolver", id=agotado.prestamo_ana)   # prestados = 0
    status, cuerpo = agotado("DELETE", "/items/{id}", id="cien")
    assert status == 409 and "reservas" in cuerpo["error"]


def test_reservas_con_redis_caido_usan_dynamodb(agotado, monkeypatch):
    """Sin Redis la fila se arma desde el GSI3 y el flujo sigue funcionando."""
    import redis as redis_lib

    class RedisCaido:
        def __getattr__(self, _nombre):
            def falla(*_a, **_k):
                raise redis_lib.ConnectionError("sin conexión")
            return falla

    monkeypatch.setattr(cache, "_get_client", lambda: RedisCaido())
    _, r1 = reservar(agotado, "beto")
    _, r2 = reservar(agotado, "caro")
    assert (r1["posicion"], r2["posicion"]) == (1, 2)
    agotado("PUT", "/prestamos/{id}/devolver", id=agotado.prestamo_ana)
    assert agotado("POST", "/prestamos", body={"usuario_id": "caro", "item_id": "cien"})[0] == 409
    assert agotado("POST", "/prestamos", body={"usuario_id": "beto", "item_id": "cien"})[0] == 201
