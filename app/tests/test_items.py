"""
test_items.py
─────────────
Tests de las rutas de libros (/items) y del enrutador de la Lambda.
moto simula DynamoDB y fakeredis simula Redis: no requiere el emulador.
"""
import json

import pytest
import redis

import cache

LIBRO = {
    "id": "cien-anios",
    "titulo": "Cien años de soledad",
    "autor": "Gabriel García Márquez",
    "anio": 1967,
    "isbn": "978-0307474728",
    "genero": "Novela",
    "ejemplares": 3,
}


def crear(api, **cambios):
    return api("POST", "/items", body={**LIBRO, **cambios})


# ── POST /items (Write-Through) ───────────────────────────────────────

def test_crear_libro_retorna_201(api):
    status, cuerpo = crear(api)
    assert status == 201
    assert cuerpo["id"] == "cien-anios"
    assert cuerpo["titulo"] == "Cien años de soledad"
    assert cuerpo["disponibles"] == 3 and cuerpo["prestados"] == 0
    assert cuerpo["disponible"] is True
    assert "PK" not in cuerpo and "GSI1PK" not in cuerpo


def test_crear_libro_modelo_single_table(api, tabla_dynamodb):
    crear(api)
    item = tabla_dynamodb.get_item(Key={"PK": "ITEM#cien-anios", "SK": "METADATA"})["Item"]
    assert item["tipo"] == "LIBRO"
    assert item["GSI1PK"] == "AUTOR#GABRIEL GARCIA MARQUEZ"
    assert item["GSI1SK"] == "ANIO#1967#ITEM#cien-anios"
    assert item["GSI2PK"] == "TIPO#LIBRO"
    assert item["GSI2SK"] == "TITULO#CIEN ANOS DE SOLEDAD#cien-anios"


def test_crear_libro_write_through_con_ttl(api, redis_fake):
    crear(api)
    clave = cache.cache_key_item("cien-anios")
    assert json.loads(redis_fake.get(clave))["titulo"] == "Cien años de soledad"
    assert 0 < redis_fake.ttl(clave) <= 300


def test_crear_libro_genera_id(api):
    datos = {k: v for k, v in LIBRO.items() if k != "id"}
    status, cuerpo = api("POST", "/items", body=datos)
    assert status == 201 and cuerpo["id"]


def test_crear_libro_duplicado_retorna_409(api):
    crear(api)
    assert crear(api)[0] == 409


def test_crear_libro_json_invalido_retorna_400(api):
    status, cuerpo = api("POST", "/items", body="no es json {{{")
    assert status == 400 and "JSON" in cuerpo["error"]


def test_crear_libro_sin_content_type_retorna_400(api):
    status, cuerpo = api("POST", "/items", body=LIBRO, headers={"Content-Type": "text/plain"})
    assert status == 400 and "Content-Type" in cuerpo["error"]


def test_crear_libro_content_type_en_minusculas(api):
    status, _ = api("POST", "/items", body=LIBRO,
                    headers={"content-type": "application/json; charset=utf-8"})
    assert status == 201


@pytest.mark.parametrize("datos, fragmento", [
    ({"autor": "Anónimo"}, "titulo"),
    ({"titulo": "Sin autor"}, "autor"),
    ({"titulo": "   ", "autor": "X"}, "titulo"),
    ({"titulo": "T", "autor": "A", "anio": "1967"}, "anio"),
    ({"titulo": "T", "autor": "A", "anio": 99999}, "anio"),
    ({"titulo": "T", "autor": "A", "ejemplares": -1}, "ejemplares"),
    ({"titulo": "T", "autor": "A", "id": "con espacios"}, "id"),
    (["no", "es", "objeto"], "objeto"),
])
def test_crear_libro_datos_invalidos_retorna_400(api, datos, fragmento):
    status, cuerpo = api("POST", "/items", body=datos)
    assert status == 400 and fragmento in cuerpo["error"]


def test_crear_libro_body_base64(tabla_dynamodb, parchear_redis):
    import base64
    import handler
    evento = {"httpMethod": "POST", "resource": "/items",
              "headers": {"Content-Type": "application/json"}, "isBase64Encoded": True,
              "body": base64.b64encode(json.dumps(LIBRO).encode()).decode()}
    assert handler.main(evento, None)["statusCode"] == 201


# ── GET /items (listado por GSI2 / GSI1, paginado) ────────────────────

def test_listar_libros_ordenados_por_titulo(api):
    crear(api)
    crear(api, id="maria", titulo="María", autor="Jorge Isaacs", anio=1867)
    crear(api, id="vorag", titulo="La vorágine", autor="José Eustasio Rivera", anio=1924)
    status, cuerpo = api("GET", "/items")
    assert status == 200
    assert [i["id"] for i in cuerpo["items"]] == ["cien-anios", "vorag", "maria"]
    assert cuerpo["siguiente"] is None
    assert cuerpo["_fuente"] == "dynamodb"


def test_listar_libros_paginado(api):
    for n in range(3):
        crear(api, id=f"l{n}", titulo=f"Libro {n}")
    _, pagina1 = api("GET", "/items", query={"limite": "2"})
    assert len(pagina1["items"]) == 2 and pagina1["siguiente"]
    _, pagina2 = api("GET", "/items", query={"limite": "2", "siguiente": pagina1["siguiente"]})
    assert [i["id"] for i in pagina2["items"]] == ["l2"]


def test_listar_por_autor_usa_gsi1(api):
    crear(api, id="amor", titulo="El amor en los tiempos del cólera", anio=1985)
    crear(api)
    crear(api, id="vorag", titulo="La vorágine", autor="José Eustasio Rivera", anio=1924)
    status, cuerpo = api("GET", "/items", query={"autor": "gabriel garcia marquez"})
    assert status == 200
    assert [i["id"] for i in cuerpo["items"]] == ["cien-anios", "amor"]  # por año


@pytest.mark.parametrize("query", [{"limite": "0"}, {"limite": "abc"}, {"siguiente": "%%%"}])
def test_listar_parametros_invalidos_retorna_400(api, query):
    assert api("GET", "/items", query=query)[0] == 400


# ── GET /items/{id} (Cache-Aside) ─────────────────────────────────────

def test_obtener_miss_dynamodb_luego_hit_cache(api, tabla_dynamodb, redis_fake):
    tabla_dynamodb.put_item(Item={"PK": "ITEM#l1", "SK": "METADATA", "id": "l1",
                                  "titulo": "La vorágine", "autor": "José Eustasio Rivera",
                                  "anio": 1924, "disponibles": 1})
    status, primera = api("GET", "/items/{id}", id="l1")
    assert status == 200 and primera["_fuente"] == "dynamodb"
    assert primera["anio"] == 1924
    assert 0 < redis_fake.ttl(cache.cache_key_item("l1")) <= 300   # r.setex(key, 300, ...)

    _, segunda = api("GET", "/items/{id}", id="l1")
    assert segunda["_fuente"] == "cache" and segunda["titulo"] == "La vorágine"


def test_obtener_tras_crear_es_hit(api):
    crear(api)
    assert api("GET", "/items/{id}", id="cien-anios")[1]["_fuente"] == "cache"


def test_obtener_hit_no_consulta_dynamodb(api, redis_fake, monkeypatch):
    import libros
    redis_fake.setex(cache.cache_key_item("b2"), 300, json.dumps({"id": "b2", "titulo": "María"}))

    def falla(*_a, **_k):
        raise AssertionError("No debería leer DynamoDB en un hit")
    monkeypatch.setattr(libros, "tabla", falla)
    _, cuerpo = api("GET", "/items/{id}", id="b2")
    assert cuerpo["_fuente"] == "cache"


def test_obtener_ttl_expirado_vuelve_a_dynamodb(api, redis_fake):
    crear(api)
    redis_fake.delete(cache.cache_key_item("cien-anios"))   # equivale a la expiración
    assert api("GET", "/items/{id}", id="cien-anios")[1]["_fuente"] == "dynamodb"


def test_obtener_inexistente_retorna_404(api):
    assert api("GET", "/items/{id}", id="no-existe")[0] == 404


def test_obtener_id_invalido_retorna_400(api):
    assert api("GET", "/items/{id}", id="id con espacios")[0] == 400


# ── PUT /items/{id} (Write-Through) ───────────────────────────────────

def test_actualizar_libro_y_cache(api, tabla_dynamodb, redis_fake):
    crear(api)
    status, cuerpo = api("PUT", "/items/{id}", id="cien-anios",
                         body={"genero": "Realismo mágico", "ejemplares": 5})
    assert status == 200
    assert cuerpo["genero"] == "Realismo mágico" and cuerpo["disponibles"] == 5
    assert cuerpo["titulo"] == "Cien años de soledad"   # lo no enviado se conserva
    # Write-Through: la caché ya tiene el valor nuevo
    assert json.loads(redis_fake.get(cache.cache_key_item("cien-anios")))["genero"] == "Realismo mágico"
    _, leido = api("GET", "/items/{id}", id="cien-anios")
    assert leido["_fuente"] == "cache" and leido["ejemplares"] == 5


def test_actualizar_autor_recalcula_gsi(api, tabla_dynamodb):
    crear(api)
    api("PUT", "/items/{id}", id="cien-anios", body={"autor": "G. G. Márquez"})
    item = tabla_dynamodb.get_item(Key={"PK": "ITEM#cien-anios", "SK": "METADATA"})["Item"]
    assert item["GSI1PK"] == "AUTOR#G. G. MARQUEZ"


@pytest.mark.parametrize("body, status", [
    ({}, 400), ({"id": "otro"}, 400), ({"titulo": ""}, 400), ({"anio": "x"}, 400),
])
def test_actualizar_invalido(api, body, status):
    crear(api)
    assert api("PUT", "/items/{id}", id="cien-anios", body=body)[0] == status


def test_actualizar_inexistente_retorna_404(api):
    assert api("PUT", "/items/{id}", id="nope", body={"genero": "X"})[0] == 404


def test_actualizar_ejemplares_menor_que_prestados_retorna_409(api):
    crear(api, ejemplares=1)
    api("POST", "/usuarios", body={"id": "u1", "nombre": "Ana", "email": "ana@unimag.edu.co"})
    api("POST", "/prestamos", body={"usuario_id": "u1", "item_id": "cien-anios"})
    assert api("PUT", "/items/{id}", id="cien-anios", body={"ejemplares": 0})[0] == 409


# ── DELETE /items/{id} (invalidación) ─────────────────────────────────

def test_eliminar_libro_invalida_cache(api, tabla_dynamodb, redis_fake):
    crear(api)
    api("GET", "/items/{id}", id="cien-anios")
    assert redis_fake.get(cache.cache_key_item("cien-anios")) is not None

    status, _ = api("DELETE", "/items/{id}", id="cien-anios")
    assert status == 200
    assert redis_fake.get(cache.cache_key_item("cien-anios")) is None
    assert redis_fake.zscore(cache.CLAVE_RANKING, "cien-anios") is None
    assert "Item" not in tabla_dynamodb.get_item(Key={"PK": "ITEM#cien-anios", "SK": "METADATA"})
    assert api("GET", "/items/{id}", id="cien-anios")[0] == 404


def test_eliminar_inexistente_retorna_404(api):
    assert api("DELETE", "/items/{id}", id="nope")[0] == 404


def test_eliminar_con_prestamos_activos_retorna_409(api):
    crear(api)
    api("POST", "/usuarios", body={"id": "u1", "nombre": "Ana", "email": "ana@unimag.edu.co"})
    api("POST", "/prestamos", body={"usuario_id": "u1", "item_id": "cien-anios"})
    assert api("DELETE", "/items/{id}", id="cien-anios")[0] == 409


# ── GET /items/populares (Sorted Set) ─────────────────────────────────

def test_populares_por_consultas(api):
    crear(api)
    crear(api, id="maria", titulo="María", autor="Jorge Isaacs")
    for _ in range(3):
        api("GET", "/items/{id}", id="maria")
    api("GET", "/items/{id}", id="cien-anios")
    status, cuerpo = api("GET", "/items/populares", query={"n": "5"})
    assert status == 200 and cuerpo["_fuente"] == "cache"
    assert cuerpo["items"] == [{"id": "maria", "consultas": 3}, {"id": "cien-anios", "consultas": 1}]


def test_populares_por_prestamos(api):
    crear(api)
    api("POST", "/usuarios", body={"id": "u1", "nombre": "Ana", "email": "ana@unimag.edu.co"})
    api("POST", "/prestamos", body={"usuario_id": "u1", "item_id": "cien-anios"})
    _, cuerpo = api("GET", "/items/populares", query={"por": "prestamos"})
    assert cuerpo["items"] == [{"id": "cien-anios", "prestamos": 1}]


@pytest.mark.parametrize("query", [{"n": "0"}, {"n": "abc"}, {"n": "999"}, {"por": "likes"}])
def test_populares_parametros_invalidos(api, query):
    assert api("GET", "/items/populares", query=query)[0] == 400


# ── Enrutador ─────────────────────────────────────────────────────────

def test_metodo_no_soportado_retorna_405(api):
    assert api("PATCH", "/items/{id}", id="x")[0] == 405


def test_ruta_desconocida_retorna_404(api):
    assert api("GET", "/libros")[0] == 404


def test_respuestas_incluyen_cors(tabla_dynamodb, parchear_redis):
    import handler
    respuesta = handler.main({"httpMethod": "GET", "resource": "/items"}, None)
    assert respuesta["headers"]["Access-Control-Allow-Origin"] == "*"


# ── Modo degradado: Redis caído → solo DynamoDB ───────────────────────

class RedisCaido:
    def __getattr__(self, _nombre):
        def falla(*_a, **_k):
            raise redis.ConnectionError("Redis no disponible")
        return falla


def test_redis_caido_crud_sigue_con_dynamodb(api, monkeypatch):
    monkeypatch.setattr(cache, "_get_client", lambda: RedisCaido())
    assert crear(api)[0] == 201
    status, cuerpo = api("GET", "/items/{id}", id="cien-anios")
    assert status == 200 and cuerpo["_fuente"] == "dynamodb"
    assert api("PUT", "/items/{id}", id="cien-anios", body={"genero": "X"})[0] == 200
    assert api("DELETE", "/items/{id}", id="cien-anios")[0] == 200


def test_redis_caido_populares_retorna_503(api, monkeypatch):
    monkeypatch.setattr(cache, "_get_client", lambda: RedisCaido())
    assert api("GET", "/items/populares")[0] == 503


def test_error_dynamodb_retorna_500(api, monkeypatch):
    from botocore.exceptions import ClientError
    import libros

    class TablaRota:
        def get_item(self, **_k):
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "GetItem")
    monkeypatch.setattr(libros, "tabla", lambda: TablaRota())
    assert api("GET", "/items/{id}", id="x")[0] == 500


def test_enrutador_tiene_28_rutas_mas_health():
    """28 rutas en ItemsHandler + GET /health en su propia Lambda = 29 endpoints."""
    import handler
    assert len(handler.RUTAS) == 28



def test_body_no_utf8_devuelve_400(tabla_dynamodb, parchear_redis):
    """Un cuerpo en Latin-1 (p. ej. curl desde Git Bash en Windows) no debe dar 500."""
    import base64
    import handler
    crudo = '{"titulo":"Cien años","autor":"García"}'.encode("latin-1")
    evento = {"httpMethod": "POST", "resource": "/items",
              "headers": {"Content-Type": "application/json"}, "isBase64Encoded": True,
              "body": base64.b64encode(crudo).decode()}
    r = handler.main(evento, None)
    assert r["statusCode"] == 400 and "UTF-8" in json.loads(r["body"])["error"]
