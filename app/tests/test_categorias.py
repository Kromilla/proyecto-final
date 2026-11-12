"""
test_categorias.py
──────────────────
Tests de GET /items/buscar, GET /categorias y GET /categorias/{nombre}/items,
y de la invalidación de esos cachés al crear, editar o borrar libros.
"""
import json

import pytest

import cache


def libro(api, item_id, titulo, categoria=None, **extra):
    datos = {"id": item_id, "titulo": titulo, "autor": "Autor", **extra}
    if categoria:
        datos["categoria"] = categoria
    return api("POST", "/items", body=datos)


@pytest.fixture
def catalogo(api):
    libro(api, "cien", "Cien años de soledad", "Novela")
    libro(api, "amor", "El amor en los tiempos del cólera", "Novela")
    libro(api, "cosmos", "Cosmos", "Ciencia")
    libro(api, "breve", "Breve historia del tiempo", "Ciencia")
    libro(api, "sin", "Sin categoría")
    return api


# ── GET /items/buscar (Cache-Aside de resultados) ─────────────────────

def test_buscar_sin_mayusculas_ni_tildes(catalogo):
    status, cuerpo = catalogo("GET", "/items/buscar", query={"q": "TIEMPO"})
    assert status == 200 and cuerpo["_fuente"] == "dynamodb"
    assert {i["id"] for i in cuerpo["items"]} == {"amor", "breve"}
    assert set(cuerpo["items"][0]) <= {"id", "titulo", "autor", "anio", "categoria"}
    _, otra = catalogo("GET", "/items/buscar", query={"q": "anos de"})
    assert [i["id"] for i in otra["items"]] == ["cien"]        # "años" ≈ "anos"


def test_buscar_segunda_vez_viene_de_cache_con_ttl(catalogo, redis_fake):
    catalogo("GET", "/items/buscar", query={"q": "tiempo"})
    _, cuerpo = catalogo("GET", "/items/buscar", query={"q": "Tiempo"})
    assert cuerpo["_fuente"] == "cache" and cuerpo["total"] == 2
    clave = cache.cache_key_busqueda("TIEMPO")
    assert 0 < redis_fake.ttl(clave) <= 300
    assert clave in redis_fake.smembers(cache.CLAVE_INDICE_BUSQUEDAS)


def test_buscar_sin_resultados_tambien_se_cachea(catalogo):
    assert catalogo("GET", "/items/buscar", query={"q": "xyz"})[1]["total"] == 0
    assert catalogo("GET", "/items/buscar", query={"q": "xyz"})[1]["_fuente"] == "cache"


@pytest.mark.parametrize("operacion", ["crear", "editar", "borrar"])
def test_cambios_en_libros_invalidan_busquedas(catalogo, redis_fake, operacion):
    catalogo("GET", "/items/buscar", query={"q": "tiempo"})
    if operacion == "crear":
        libro(catalogo, "nuevo", "El tiempo entre costuras")
    elif operacion == "editar":
        catalogo("PUT", "/items/{id}", id="cosmos", body={"titulo": "Cosmos y tiempo"})
    else:
        catalogo("DELETE", "/items/{id}", id="breve")
    assert redis_fake.get(cache.cache_key_busqueda("TIEMPO")) is None
    _, cuerpo = catalogo("GET", "/items/buscar", query={"q": "tiempo"})
    assert cuerpo["_fuente"] == "dynamodb"
    esperado = {"crear": 3, "editar": 3, "borrar": 1}[operacion]
    assert cuerpo["total"] == esperado


@pytest.mark.parametrize("query", [{}, {"q": "a"}, {"q": "x" * 101}])
def test_buscar_q_invalido_400(api, query):
    assert api("GET", "/items/buscar", query=query)[0] == 400


def test_ruta_buscar_no_se_confunde_con_id(catalogo):
    """El recurso /items/buscar va a la búsqueda; /items/{id} con id 'buscar' es un libro."""
    assert catalogo("GET", "/items/buscar", query={"q": "cosmos"})[0] == 200
    assert catalogo("GET", "/items/{id}", id="buscar")[0] == 404


# ── GET /categorias (Set de Redis, Cache-Aside) ───────────────────────

def test_listar_categorias_miss_y_hit(catalogo, redis_fake):
    status, cuerpo = catalogo("GET", "/categorias")
    assert status == 200 and cuerpo["_fuente"] == "dynamodb"
    assert cuerpo["categorias"] == ["Ciencia", "Novela"]
    assert redis_fake.smembers(cache.CLAVE_CATEGORIAS) == {"Ciencia", "Novela"}
    assert 0 < redis_fake.ttl(cache.CLAVE_CATEGORIAS) <= 300
    _, otra = catalogo("GET", "/categorias")
    assert otra["_fuente"] == "cache" and otra["categorias"] == ["Ciencia", "Novela"]


def test_contador_de_categoria_en_dynamodb(catalogo, tabla_dynamodb):
    item = tabla_dynamodb.get_item(Key={"PK": "CATEGORIA#CIENCIA", "SK": "METADATA"})["Item"]
    assert item["libros"] == 2 and item["nombre"] == "Ciencia"
    catalogo("PUT", "/items/{id}", id="cosmos", body={"categoria": "Novela"})
    assert tabla_dynamodb.get_item(Key={"PK": "CATEGORIA#CIENCIA", "SK": "METADATA"})["Item"]["libros"] == 1
    assert tabla_dynamodb.get_item(Key={"PK": "CATEGORIA#NOVELA", "SK": "METADATA"})["Item"]["libros"] == 3


def test_categoria_vacia_desaparece_e_invalida_set(catalogo, redis_fake):
    catalogo("GET", "/categorias")
    catalogo("DELETE", "/items/{id}", id="cosmos")
    assert not redis_fake.exists(cache.CLAVE_CATEGORIAS)       # invalidado
    catalogo("DELETE", "/items/{id}", id="breve")
    _, cuerpo = catalogo("GET", "/categorias")
    assert cuerpo["categorias"] == ["Novela"]


def test_nueva_categoria_invalida_set(catalogo, redis_fake):
    catalogo("GET", "/categorias")
    libro(catalogo, "poema", "Veinte poemas de amor", "Poesía")
    _, cuerpo = catalogo("GET", "/categorias")
    assert cuerpo["_fuente"] == "dynamodb" and "Poesía" in cuerpo["categorias"]


def test_categorias_catalogo_vacio(api):
    status, cuerpo = api("GET", "/categorias")
    assert status == 200 and cuerpo["categorias"] == []


@pytest.mark.parametrize("valor", ["", "x" * 51])
def test_categoria_invalida_al_crear_400(api, valor):
    status, _ = api("POST", "/items", body={"titulo": "Libro", "autor": "A", "categoria": valor})
    assert status == 400


# ── GET /categorias/{nombre}/items (GSI3) ─────────────────────────────

def test_libros_de_categoria_ordenados_por_titulo(catalogo):
    status, cuerpo = catalogo("GET", "/categorias/{nombre}/items", nombre="ciencia")
    assert status == 200 and cuerpo["categoria"] == "Ciencia"
    assert [i["id"] for i in cuerpo["items"]] == ["breve", "cosmos"]


def test_libros_de_categoria_con_tildes_y_url_encoding(catalogo):
    libro(catalogo, "poema", "Veinte poemas de amor", "Poesía")
    _, cuerpo = catalogo("GET", "/categorias/{nombre}/items", nombre="Poes%C3%ADa")
    assert [i["id"] for i in cuerpo["items"]] == ["poema"]


def test_libros_de_categoria_paginado(catalogo):
    _, pagina = catalogo("GET", "/categorias/{nombre}/items", nombre="Novela", query={"limite": "1"})
    assert len(pagina["items"]) == 1 and pagina["siguiente"]


def test_libros_de_categoria_inexistente_404(catalogo):
    assert catalogo("GET", "/categorias/{nombre}/items", nombre="Terror")[0] == 404


def test_libros_de_categoria_nombre_largo_400(catalogo):
    assert catalogo("GET", "/categorias/{nombre}/items", nombre="x" * 60)[0] == 400


def test_libro_muestra_su_categoria(catalogo):
    _, cuerpo = catalogo("GET", "/items/{id}", id="cosmos")
    assert cuerpo["categoria"] == "Ciencia"
    assert "titulo_busqueda" not in cuerpo and "GSI3PK" not in cuerpo
