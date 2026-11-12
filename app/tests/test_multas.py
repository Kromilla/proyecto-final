"""
test_multas.py
──────────────
Tests de POST /multas, GET /usuarios/{id}/multas y de la multa automática
al devolver un libro vencido.
"""
import pytest

import cache


@pytest.fixture
def usuario(api):
    api("POST", "/usuarios", body={"id": "ana", "nombre": "Ana", "email": "ana@unimag.edu.co"})
    return api


def multar(api, **datos):
    return api("POST", "/multas", body={"usuario_id": "ana", "monto": 5000,
                                        "motivo": "Libro dañado", **datos})


# ── POST /multas ──────────────────────────────────────────────────────

def test_crear_multa_y_actualizar_saldo(usuario, tabla_dynamodb):
    status, multa = multar(usuario)
    assert status == 201
    assert multa["monto"] == 5000 and multa["estado"] == "PENDIENTE" and multa["origen"] == "MANUAL"
    item = tabla_dynamodb.query(KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                                ExpressionAttributeValues={":pk": "USER#ana", ":sk": "FINE#"})["Items"]
    assert len(item) == 1 and item[0]["GSI1PK"] == f"MULTA#{multa['id']}"
    perfil = tabla_dynamodb.get_item(Key={"PK": "USER#ana", "SK": "PROFILE"})["Item"]
    assert perfil["saldo_multas"] == 5000


def test_crear_multa_invalida_cache_del_usuario(usuario, redis_fake):
    usuario("GET", "/usuarios/{id}/multas", id="ana")
    usuario("GET", "/usuarios/{id}", id="ana")
    multar(usuario)
    assert redis_fake.get(cache.cache_key_multas_usuario("ana")) is None
    _, perfil = usuario("GET", "/usuarios/{id}", id="ana")
    assert perfil["_fuente"] == "dynamodb" and perfil["saldo_multas"] == 5000


def test_crear_multa_asociada_a_prestamo(usuario):
    usuario("POST", "/items", body={"id": "l1", "titulo": "T", "autor": "A"})
    _, prestamo = usuario("POST", "/prestamos", body={"usuario_id": "ana", "item_id": "l1"})
    status, multa = multar(usuario, prestamo_id=prestamo["id"])
    assert status == 201 and multa["prestamo_id"] == prestamo["id"]


def test_multa_prestamo_de_otro_usuario_400(usuario):
    usuario("POST", "/usuarios", body={"id": "beto", "nombre": "Beto", "email": "b@unimag.edu.co"})
    usuario("POST", "/items", body={"id": "l1", "titulo": "T", "autor": "A"})
    _, prestamo = usuario("POST", "/prestamos", body={"usuario_id": "beto", "item_id": "l1"})
    assert multar(usuario, prestamo_id=prestamo["id"])[0] == 400


def test_multa_prestamo_inexistente_404(usuario):
    assert multar(usuario, prestamo_id="nope")[0] == 404


def test_multa_usuario_inexistente_404(api):
    assert api("POST", "/multas", body={"usuario_id": "nadie", "monto": 1, "motivo": "x"})[0] == 404


@pytest.mark.parametrize("cambios", [{"monto": 0}, {"monto": "mil"}, {"monto": 10**9},
                                     {"motivo": ""}, {"usuario_id": "a b"}])
def test_multa_datos_invalidos_400(usuario, cambios):
    assert multar(usuario, **cambios)[0] == 400


def test_multa_sin_monto_400(usuario):
    assert usuario("POST", "/multas", body={"usuario_id": "ana", "motivo": "x"})[0] == 400


# ── GET /usuarios/{id}/multas (Query por PK + Cache-Aside) ───────────

def test_listar_multas_miss_y_hit(usuario):
    multar(usuario, monto=2000)
    multar(usuario, monto=3000)
    status, cuerpo = usuario("GET", "/usuarios/{id}/multas", id="ana")
    assert status == 200 and cuerpo["_fuente"] == "dynamodb"
    assert cuerpo["total"] == 2 and cuerpo["saldo_pendiente"] == 5000
    assert usuario("GET", "/usuarios/{id}/multas", id="ana")[1]["_fuente"] == "cache"


def test_listar_multas_usuario_inexistente_404(api):
    assert api("GET", "/usuarios/{id}/multas", id="nadie")[0] == 404


# ── Multa automática al devolver tarde ────────────────────────────────

def test_devolucion_tardia_genera_multa(usuario, viajar_en_el_tiempo, tabla_dynamodb):
    usuario("POST", "/items", body={"id": "l1", "titulo": "T", "autor": "A"})
    _, prestamo = usuario("POST", "/prestamos", body={"usuario_id": "ana", "item_id": "l1"})
    viajar_en_el_tiempo(dias=17)                     # 14 días de plazo → 3 de retraso
    status, cuerpo = usuario("PUT", "/prestamos/{id}/devolver", id=prestamo["id"])
    assert status == 200 and cuerpo["dias_retraso"] == 3
    assert cuerpo["multa"]["monto"] == 3000 and cuerpo["multa"]["origen"] == "AUTOMATICA"

    _, multas = usuario("GET", "/usuarios/{id}/multas", id="ana")
    assert multas["total"] == 1 and multas["saldo_pendiente"] == 3000
    perfil = tabla_dynamodb.get_item(Key={"PK": "USER#ana", "SK": "PROFILE"})["Item"]
    assert perfil["saldo_multas"] == 3000 and perfil["prestamos_activos"] == 0


def test_devolucion_a_tiempo_no_genera_multa(usuario):
    usuario("POST", "/items", body={"id": "l1", "titulo": "T", "autor": "A"})
    _, prestamo = usuario("POST", "/prestamos", body={"usuario_id": "ana", "item_id": "l1"})
    _, cuerpo = usuario("PUT", "/prestamos/{id}/devolver", id=prestamo["id"])
    assert cuerpo["multa"] is None and cuerpo["dias_retraso"] == 0


def test_eliminar_usuario_borra_sus_multas(usuario):
    multar(usuario)
    _, cuerpo = usuario("DELETE", "/usuarios/{id}", id="ana")
    assert cuerpo["multas_eliminadas"] == 1
