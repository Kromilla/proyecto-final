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


def con_prestamo_devuelto(api, usuario="ana", item="l-multa"):
    """Deja al usuario con un préstamo devuelto, para poder registrar una multa."""
    api("POST", "/items", body={"id": item, "titulo": "T", "autor": "A"})
    _, prestamo = api("POST", "/prestamos", body={"usuario_id": usuario, "item_id": item})
    api("PUT", "/prestamos/{id}/devolver", id=prestamo["id"])
    return prestamo


# ── POST /multas ──────────────────────────────────────────────────────

def test_crear_multa_y_actualizar_saldo(usuario, tabla_dynamodb):
    con_prestamo_devuelto(usuario)
    status, multa = multar(usuario)
    assert status == 201
    assert multa["monto"] == 5000 and multa["estado"] == "PENDIENTE" and multa["origen"] == "MANUAL"
    item = tabla_dynamodb.query(KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                                ExpressionAttributeValues={":pk": "USER#ana", ":sk": "FINE#"})["Items"]
    assert len(item) == 1 and item[0]["GSI1PK"] == f"MULTA#{multa['id']}"
    perfil = tabla_dynamodb.get_item(Key={"PK": "USER#ana", "SK": "PROFILE"})["Item"]
    assert perfil["saldo_multas"] == 5000


def test_crear_multa_invalida_cache_del_usuario(usuario, redis_fake):
    con_prestamo_devuelto(usuario)
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
    assert api("POST", "/multas", body={"usuario_id": "nadie", "monto": 1000, "motivo": "x"})[0] == 404


@pytest.mark.parametrize("cambios", [{"monto": 0}, {"monto": "mil"}, {"monto": 10**9},
                                     {"monto": 52}, {"monto": 999}, {"monto": 1050}, {"monto": 1500},
                                     {"monto": 500100}, {"motivo": ""}, {"usuario_id": "a b"}])
def test_multa_datos_invalidos_400(usuario, cambios):
    assert multar(usuario, **cambios)[0] == 400


def test_multa_sin_monto_400(usuario):
    assert usuario("POST", "/multas", body={"usuario_id": "ana", "motivo": "x"})[0] == 400


@pytest.mark.parametrize("monto", [52, 999, 1050, 1500, 500100, 1, 0])
def test_multa_monto_fuera_de_regla_400(usuario, monto):
    status, cuerpo = multar(usuario, monto=monto)
    assert status == 400
    assert "1000" in cuerpo["error"] and "500000" in cuerpo["error"] and "múltiplos de 1000" in cuerpo["error"]


@pytest.mark.parametrize("monto", [1000, 2000, 15000, 500000])
def test_multa_monto_valido_limites(usuario, monto):
    con_prestamo_devuelto(usuario, item=f"l-monto-{monto}")
    status, multa = multar(usuario, monto=monto)
    assert status == 201 and multa["monto"] == monto


def test_multa_sin_prestamos_retorna_409(usuario, tabla_dynamodb):
    status, cuerpo = multar(usuario)
    assert status == 409 and "préstamos" in cuerpo["error"]
    perfil = tabla_dynamodb.get_item(Key={"PK": "USER#ana", "SK": "PROFILE"})["Item"]
    assert perfil["saldo_multas"] == 0
    fines = tabla_dynamodb.query(KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                                 ExpressionAttributeValues={":pk": "USER#ana", ":sk": "FINE#"})["Items"]
    assert fines == []


def test_multa_con_prestamo_devuelto_201(usuario):
    prestamo = con_prestamo_devuelto(usuario)
    status, multa = multar(usuario, prestamo_id=prestamo["id"])
    assert status == 201 and multa["prestamo_id"] == prestamo["id"]


# ── GET /usuarios/{id}/multas (Query por PK + Cache-Aside) ───────────

def test_listar_multas_miss_y_hit(usuario):
    con_prestamo_devuelto(usuario)
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
    con_prestamo_devuelto(usuario)
    multar(usuario)
    _, cuerpo = usuario("DELETE", "/usuarios/{id}", id="ana")
    assert cuerpo["multas_eliminadas"] == 1


# ── POST /multas/{id}/pagar ───────────────────────────────────────────

def test_pagar_multa_saldia_y_permite_prestar(usuario, tabla_dynamodb):
    con_prestamo_devuelto(usuario)
    _, multa = multar(usuario, monto=2000)
    status, pagada = usuario("POST", "/multas/{id}/pagar", id=multa["id"])
    assert status == 200
    assert pagada["estado"] == "PAGADA" and pagada["fecha_pago"]
    assert pagada["id"] == multa["id"]
    perfil = tabla_dynamodb.get_item(Key={"PK": "USER#ana", "SK": "PROFILE"})["Item"]
    assert perfil["saldo_multas"] == 0
    _, lista = usuario("GET", "/usuarios/{id}/multas", id="ana")
    assert lista["saldo_pendiente"] == 0 and lista["multas"][0]["estado"] == "PAGADA"
    # Ya puede volver a llevarse un libro.
    usuario("POST", "/items", body={"id": "l-despues", "titulo": "T", "autor": "A"})
    status, prestamo = usuario("POST", "/prestamos", body={"usuario_id": "ana", "item_id": "l-despues"})
    assert status == 201 and prestamo["estado"] == "ACTIVO"


def test_pagar_multa_ya_pagada_es_idempotente(usuario, tabla_dynamodb):
    con_prestamo_devuelto(usuario)
    _, multa = multar(usuario, monto=2000)
    _, primera = usuario("POST", "/multas/{id}/pagar", id=multa["id"])
    status, segunda = usuario("POST", "/multas/{id}/pagar", id=multa["id"])
    assert status == 200
    assert segunda["estado"] == "PAGADA" and segunda["fecha_pago"] == primera["fecha_pago"]
    perfil = tabla_dynamodb.get_item(Key={"PK": "USER#ana", "SK": "PROFILE"})["Item"]
    assert perfil["saldo_multas"] == 0   # no se descuenta dos veces


def test_pagar_multa_inexistente_404(usuario):
    assert usuario("POST", "/multas/{id}/pagar", id="nope")[0] == 404


def test_pagar_multa_invalida_cache(usuario, redis_fake):
    con_prestamo_devuelto(usuario)
    _, multa = multar(usuario, monto=2000)
    usuario("GET", "/usuarios/{id}/multas", id="ana")
    usuario("GET", "/usuarios/{id}", id="ana")
    usuario("POST", "/multas/{id}/pagar", id=multa["id"])
    assert redis_fake.get(cache.cache_key_multas_usuario("ana")) is None
    _, perfil = usuario("GET", "/usuarios/{id}", id="ana")
    assert perfil["_fuente"] == "dynamodb" and perfil["saldo_multas"] == 0


# ── Idempotencia de POST /multas ──────────────────────────────────────

def test_crear_multa_con_misma_idempotency_key_no_duplica(usuario, tabla_dynamodb):
    con_prestamo_devuelto(usuario)
    headers = {"Content-Type": "application/json", "Idempotency-Key": "multa-ana-1"}
    s1, m1 = usuario("POST", "/multas", body={"usuario_id": "ana", "monto": 2000,
                                              "motivo": "Libro dañado"}, headers=headers)
    s2, m2 = usuario("POST", "/multas", body={"usuario_id": "ana", "monto": 2000,
                                              "motivo": "Libro dañado"}, headers=headers)
    assert s1 == s2 == 201 and m1["id"] == m2["id"]
    fines = tabla_dynamodb.query(KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                                 ExpressionAttributeValues={":pk": "USER#ana", ":sk": "FINE#"})["Items"]
    assert len(fines) == 1
    perfil = tabla_dynamodb.get_item(Key={"PK": "USER#ana", "SK": "PROFILE"})["Item"]
    assert perfil["saldo_multas"] == 2000


def test_multa_duplicada_mismo_prestamo_y_motivo_409(usuario):
    prestamo = con_prestamo_devuelto(usuario)
    assert multar(usuario, prestamo_id=prestamo["id"], motivo="Libro dañado")[0] == 201
    status, cuerpo = multar(usuario, prestamo_id=prestamo["id"], motivo="Libro dañado")
    assert status == 409 and "mismo motivo" in cuerpo["error"]
    # Otro motivo sobre el mismo préstamo sí se admite.
    assert multar(usuario, prestamo_id=prestamo["id"], motivo="Páginas rotas", monto=1000)[0] == 201


def test_idempotency_key_invalida_400(usuario):
    con_prestamo_devuelto(usuario)
    status, cuerpo = usuario(
        "POST", "/multas",
        body={"usuario_id": "ana", "monto": 2000, "motivo": "x"},
        headers={"Content-Type": "application/json", "Idempotency-Key": "clave con espacios"},
    )
    assert status == 400 and "Idempotency-Key" in cuerpo["error"]

