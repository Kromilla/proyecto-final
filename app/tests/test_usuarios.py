"""
test_usuarios.py
────────────────
Tests de /usuarios, /usuarios/{id} y /usuarios/{id}/prestamos.
"""
import json

import pytest

import cache

ANA = {"id": "ana", "nombre": "Ana Pérez", "email": "Ana@Unimag.edu.co", "programa": "Sistemas"}


def crear_usuario(api, **cambios):
    return api("POST", "/usuarios", body={**ANA, **cambios})


def crear_libro(api, item_id="cien-anios", ejemplares=2):
    return api("POST", "/items", body={"id": item_id, "titulo": f"Libro {item_id}",
                                       "autor": "Autor", "ejemplares": ejemplares})


# ── POST /usuarios ────────────────────────────────────────────────────

def test_crear_usuario_retorna_201_y_write_through(api, tabla_dynamodb, redis_fake):
    status, cuerpo = crear_usuario(api)
    assert status == 201
    assert cuerpo["email"] == "ana@unimag.edu.co"
    assert cuerpo["prestamos_activos"] == 0 and cuerpo["max_prestamos"] == 3
    item = tabla_dynamodb.get_item(Key={"PK": "USER#ana", "SK": "PROFILE"})["Item"]
    assert item["tipo"] == "USUARIO" and item["GSI2PK"] == "TIPO#USUARIO"
    clave = cache.cache_key_usuario("ana")
    assert json.loads(redis_fake.get(clave))["nombre"] == "Ana Pérez"
    assert 0 < redis_fake.ttl(clave) <= 300


def test_crear_usuario_duplicado_retorna_409(api):
    crear_usuario(api)
    assert crear_usuario(api)[0] == 409


@pytest.mark.parametrize("body, fragmento", [
    ({"email": "a@b.co"}, "nombre"),
    ({"nombre": "Ana"}, "email"),
    ({"nombre": "Ana", "email": "no-es-correo"}, "email"),
    ({"nombre": "Ana", "email": "a@b.co", "id": "con espacio"}, "id"),
])
def test_crear_usuario_invalido_retorna_400(api, body, fragmento):
    status, cuerpo = api("POST", "/usuarios", body=body)
    assert status == 400 and fragmento in cuerpo["error"]


# ── GET /usuarios/{id} (Cache-Aside) ──────────────────────────────────

def test_obtener_usuario_miss_y_hit(api, redis_fake):
    crear_usuario(api)
    redis_fake.delete(cache.cache_key_usuario("ana"))
    assert api("GET", "/usuarios/{id}", id="ana")[1]["_fuente"] == "dynamodb"
    assert api("GET", "/usuarios/{id}", id="ana")[1]["_fuente"] == "cache"


def test_obtener_usuario_inexistente_retorna_404(api):
    assert api("GET", "/usuarios/{id}", id="nadie")[0] == 404


# ── DELETE /usuarios/{id} ─────────────────────────────────────────────

def test_eliminar_usuario_borra_particion_e_invalida(api, tabla_dynamodb, redis_fake):
    crear_usuario(api)
    crear_libro(api)
    _, prestamo = api("POST", "/prestamos", body={"usuario_id": "ana", "item_id": "cien-anios"})
    api("PUT", "/prestamos/{id}/devolver", id=prestamo["id"])
    api("GET", "/usuarios/{id}/prestamos", id="ana")          # llena la caché
    api("GET", "/usuarios/{id}", id="ana")

    status, cuerpo = api("DELETE", "/usuarios/{id}", id="ana")
    assert status == 200 and cuerpo["prestamos_eliminados"] == 1
    restantes = tabla_dynamodb.query(KeyConditionExpression="PK = :pk",
                                     ExpressionAttributeValues={":pk": "USER#ana"})["Items"]
    assert restantes == []
    assert redis_fake.get(cache.cache_key_usuario("ana")) is None
    assert redis_fake.get(cache.cache_key_prestamos_usuario("ana")) is None


def test_eliminar_usuario_con_prestamo_activo_retorna_409(api):
    crear_usuario(api)
    crear_libro(api)
    api("POST", "/prestamos", body={"usuario_id": "ana", "item_id": "cien-anios"})
    assert api("DELETE", "/usuarios/{id}", id="ana")[0] == 409


def test_eliminar_usuario_inexistente_retorna_404(api):
    assert api("DELETE", "/usuarios/{id}", id="nadie")[0] == 404


# ── GET /usuarios/{id}/prestamos (Query por PK + Cache-Aside) ─────────

def test_listar_prestamos_del_usuario(api):
    crear_usuario(api)
    crear_libro(api, "l1")
    crear_libro(api, "l2")
    _, p1 = api("POST", "/prestamos", body={"usuario_id": "ana", "item_id": "l1"})
    api("POST", "/prestamos", body={"usuario_id": "ana", "item_id": "l2"})
    api("PUT", "/prestamos/{id}/devolver", id=p1["id"])

    status, cuerpo = api("GET", "/usuarios/{id}/prestamos", id="ana")
    assert status == 200 and cuerpo["total"] == 2 and cuerpo["_fuente"] == "dynamodb"
    assert {p["item_id"] for p in cuerpo["prestamos"]} == {"l1", "l2"}

    _, activos = api("GET", "/usuarios/{id}/prestamos", id="ana", query={"estado": "activo"})
    assert activos["_fuente"] == "cache"
    assert [p["item_id"] for p in activos["prestamos"]] == ["l2"]
    _, devueltos = api("GET", "/usuarios/{id}/prestamos", id="ana", query={"estado": "devuelto"})
    assert [p["item_id"] for p in devueltos["prestamos"]] == ["l1"]


def test_listar_prestamos_usuario_sin_prestamos(api):
    crear_usuario(api)
    status, cuerpo = api("GET", "/usuarios/{id}/prestamos", id="ana")
    assert status == 200 and cuerpo["prestamos"] == []
    assert api("GET", "/usuarios/{id}/prestamos", id="ana")[1]["_fuente"] == "cache"


def test_listar_prestamos_usuario_inexistente_404(api):
    assert api("GET", "/usuarios/{id}/prestamos", id="nadie")[0] == 404


def test_listar_prestamos_estado_invalido_400(api):
    crear_usuario(api)
    assert api("GET", "/usuarios/{id}/prestamos", id="ana", query={"estado": "x"})[0] == 400


# ── PUT /usuarios/{id} (UpdateItem + Write-Through) ───────────────────

def test_actualizar_usuario_y_cache(api, tabla_dynamodb, redis_fake):
    crear_usuario(api)
    status, cuerpo = api("PUT", "/usuarios/{id}", id="ana",
                         body={"nombre": "Ana María Pérez", "email": "AMP@unimag.edu.co"})
    assert status == 200
    assert cuerpo["nombre"] == "Ana María Pérez" and cuerpo["email"] == "amp@unimag.edu.co"
    assert cuerpo["programa"] == "Sistemas"          # lo no enviado se conserva
    item = tabla_dynamodb.get_item(Key={"PK": "USER#ana", "SK": "PROFILE"})["Item"]
    assert item["GSI2SK"] == "NOMBRE#ANA MARIA PEREZ#ana"
    # Write-Through: el GET siguiente ya es hit y trae el valor nuevo
    _, leido = api("GET", "/usuarios/{id}", id="ana")
    assert leido["_fuente"] == "cache" and leido["nombre"] == "Ana María Pérez"


def test_actualizar_usuario_no_pisa_contadores(api, tabla_dynamodb):
    crear_usuario(api)
    crear_libro(api)
    api("POST", "/prestamos", body={"usuario_id": "ana", "item_id": "cien-anios"})
    api("PUT", "/usuarios/{id}", id="ana", body={"programa": "Medicina"})
    item = tabla_dynamodb.get_item(Key={"PK": "USER#ana", "SK": "PROFILE"})["Item"]
    assert item["prestamos_activos"] == 1 and item["programa"] == "Medicina"


@pytest.mark.parametrize("body", [{}, {"prestamos_activos": 0}, {"email": "malo"},
                                  {"nombre": ""}])
def test_actualizar_usuario_invalido_400(api, body):
    crear_usuario(api)
    assert api("PUT", "/usuarios/{id}", id="ana", body=body)[0] == 400


def test_actualizar_usuario_inexistente_404(api):
    assert api("PUT", "/usuarios/{id}", id="nadie", body={"nombre": "X"})[0] == 404


# ── GET /usuarios (GSI2, paginado) ────────────────────────────────────

def test_listar_usuarios_ordenados_y_paginados(api):
    crear_usuario(api, id="u1", nombre="Zoe")
    crear_usuario(api, id="u2", nombre="Ángela")
    crear_usuario(api, id="u3", nombre="Mario")
    crear_libro(api)                                  # no debe salir en el listado
    status, pagina1 = api("GET", "/usuarios", query={"limite": "2"})
    assert status == 200
    assert [u["id"] for u in pagina1["usuarios"]] == ["u2", "u3"]
    assert pagina1["siguiente"]
    _, pagina2 = api("GET", "/usuarios", query={"limite": "2", "siguiente": pagina1["siguiente"]})
    assert [u["id"] for u in pagina2["usuarios"]] == ["u1"]


def test_listar_usuarios_limite_invalido_400(api):
    assert api("GET", "/usuarios", query={"limite": "500"})[0] == 400


def test_eliminar_usuario_con_reserva_retorna_409(api):
    crear_usuario(api)
    crear_libro(api, ejemplares=0)
    api("POST", "/reservas", body={"usuario_id": "ana", "item_id": "cien-anios"})
    assert api("DELETE", "/usuarios/{id}", id="ana")[0] == 409
