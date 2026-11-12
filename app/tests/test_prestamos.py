"""
test_prestamos.py
─────────────────
Tests de /prestamos, /prestamos/{id} y /prestamos/{id}/devolver:
transacciones en DynamoDB e invalidación del caché.
"""
import pytest

import cache


@pytest.fixture
def datos(api):
    api("POST", "/items", body={"id": "cien-anios", "titulo": "Cien años de soledad",
                                "autor": "Gabriel García Márquez", "ejemplares": 1})
    api("POST", "/usuarios", body={"id": "ana", "nombre": "Ana", "email": "ana@unimag.edu.co"})
    return api


def prestar(api, usuario="ana", item="cien-anios"):
    return api("POST", "/prestamos", body={"usuario_id": usuario, "item_id": item})


# ── POST /prestamos ───────────────────────────────────────────────────

def test_prestar_actualiza_libro_usuario_y_crea_prestamo(datos, tabla_dynamodb):
    status, prestamo = prestar(datos)
    assert status == 201
    assert prestamo["estado"] == "ACTIVO" and prestamo["titulo"] == "Cien años de soledad"
    assert prestamo["fecha_limite"] > prestamo["fecha_prestamo"][:10]

    libro = tabla_dynamodb.get_item(Key={"PK": "ITEM#cien-anios", "SK": "METADATA"})["Item"]
    assert libro["disponibles"] == 0 and libro["prestados"] == 1
    usuario = tabla_dynamodb.get_item(Key={"PK": "USER#ana", "SK": "PROFILE"})["Item"]
    assert usuario["prestamos_activos"] == 1
    items = tabla_dynamodb.query(KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                                 ExpressionAttributeValues={":pk": "USER#ana", ":sk": "LOAN#"})["Items"]
    assert len(items) == 1 and items[0]["SK"].endswith(f"#cien-anios#{prestamo['id']}")
    assert items[0]["GSI1PK"] == f"PRESTAMO#{prestamo['id']}"


def test_prestar_invalida_cache_del_libro_y_usuario(datos, redis_fake):
    datos("GET", "/usuarios/{id}", id="ana")
    datos("GET", "/usuarios/{id}/prestamos", id="ana")
    assert redis_fake.get(cache.cache_key_item("cien-anios")) is not None
    prestar(datos)
    for clave in (cache.cache_key_item("cien-anios"), cache.cache_key_usuario("ana"),
                  cache.cache_key_prestamos_usuario("ana")):
        assert redis_fake.get(clave) is None
    # La siguiente lectura trae el stock actualizado desde DynamoDB
    _, libro = datos("GET", "/items/{id}", id="cien-anios")
    assert libro["_fuente"] == "dynamodb" and libro["disponible"] is False


def test_prestar_sin_ejemplares_retorna_409(datos):
    datos("POST", "/usuarios", body={"id": "beto", "nombre": "Beto", "email": "b@unimag.edu.co"})
    prestar(datos)
    status, cuerpo = prestar(datos, usuario="beto")
    assert status == 409 and "disponibles" in cuerpo["error"]


def test_prestar_supera_maximo_retorna_409(datos):
    for n in range(4):
        datos("POST", "/items", body={"id": f"l{n}", "titulo": f"L{n}", "autor": "A"})
    for n in range(3):
        assert prestar(datos, item=f"l{n}")[0] == 201
    status, cuerpo = prestar(datos, item="l3")
    assert status == 409 and "3 préstamos" in cuerpo["error"]


@pytest.mark.parametrize("usuario, item", [("nadie", "cien-anios"), ("ana", "no-existe")])
def test_prestar_usuario_o_libro_inexistente_retorna_404(datos, usuario, item):
    assert prestar(datos, usuario=usuario, item=item)[0] == 404


@pytest.mark.parametrize("body", [{"usuario_id": "ana"}, {"item_id": "x"},
                                  {"usuario_id": "a b", "item_id": "x"}])
def test_prestar_body_invalido_retorna_400(datos, body):
    assert datos("POST", "/prestamos", body=body)[0] == 400


# ── GET /prestamos/{id} (Query sobre GSI1) ────────────────────────────

def test_obtener_prestamo(datos):
    _, prestamo = prestar(datos)
    status, cuerpo = datos("GET", "/prestamos/{id}", id=prestamo["id"])
    assert status == 200 and cuerpo["usuario_id"] == "ana" and cuerpo["estado"] == "ACTIVO"


def test_obtener_prestamo_inexistente_retorna_404(datos):
    assert datos("GET", "/prestamos/{id}", id="nope")[0] == 404


# ── PUT /prestamos/{id}/devolver ──────────────────────────────────────

def test_devolver_restituye_stock_e_invalida(datos, tabla_dynamodb, redis_fake):
    _, prestamo = prestar(datos)
    datos("GET", "/items/{id}", id="cien-anios")      # vuelve a cachear el libro
    status, cuerpo = datos("PUT", "/prestamos/{id}/devolver", id=prestamo["id"])
    assert status == 200 and cuerpo["estado"] == "DEVUELTO" and cuerpo["fecha_devolucion"]

    libro = tabla_dynamodb.get_item(Key={"PK": "ITEM#cien-anios", "SK": "METADATA"})["Item"]
    assert libro["disponibles"] == 1 and libro["prestados"] == 0
    usuario = tabla_dynamodb.get_item(Key={"PK": "USER#ana", "SK": "PROFILE"})["Item"]
    assert usuario["prestamos_activos"] == 0
    assert redis_fake.get(cache.cache_key_item("cien-anios")) is None
    assert datos("GET", "/prestamos/{id}", id=prestamo["id"])[1]["estado"] == "DEVUELTO"


def test_devolver_dos_veces_retorna_409(datos):
    _, prestamo = prestar(datos)
    datos("PUT", "/prestamos/{id}/devolver", id=prestamo["id"])
    assert datos("PUT", "/prestamos/{id}/devolver", id=prestamo["id"])[0] == 409


def test_devolver_inexistente_retorna_404(datos):
    assert datos("PUT", "/prestamos/{id}/devolver", id="nope")[0] == 404


def test_flujo_completo_libro_vuelve_a_prestarse(datos):
    _, prestamo = prestar(datos)
    datos("PUT", "/prestamos/{id}/devolver", id=prestamo["id"])
    assert prestar(datos)[0] == 201


# ── GET /prestamos?estado= (GSI3) ─────────────────────────────────────

def test_listar_prestamos_por_estado(datos, tabla_dynamodb):
    datos("POST", "/items", body={"id": "l2", "titulo": "L2", "autor": "A"})
    _, p1 = prestar(datos)
    _, p2 = prestar(datos, item="l2")
    datos("PUT", "/prestamos/{id}/devolver", id=p1["id"])

    status, activos = datos("GET", "/prestamos", query={"estado": "activo"})
    assert status == 200 and [p["id"] for p in activos["prestamos"]] == [p2["id"]]
    _, devueltos = datos("GET", "/prestamos", query={"estado": "devuelto"})
    assert [p["id"] for p in devueltos["prestamos"]] == [p1["id"]]
    _, por_defecto = datos("GET", "/prestamos")
    assert por_defecto["estado"] == "ACTIVO"
    item = tabla_dynamodb.query(IndexName="GSI1", KeyConditionExpression="GSI1PK = :g",
                                ExpressionAttributeValues={":g": f"PRESTAMO#{p1['id']}"})["Items"][0]
    assert item["GSI3PK"] == "PRESTAMOS#DEVUELTO"


def test_listar_prestamos_estado_invalido_400(datos):
    assert datos("GET", "/prestamos", query={"estado": "perdido"})[0] == 400


def test_plazo_por_defecto_14_dias(datos):
    from datetime import date
    _, prestamo = prestar(datos)
    dias = (date.fromisoformat(prestamo["fecha_limite"])
            - date.fromisoformat(prestamo["fecha_prestamo"][:10])).days
    assert dias == 14 and prestamo["renovaciones"] == 0


# ── GET /prestamos/vencidos (GSI3SK < hoy) ────────────────────────────

def test_vencidos(datos, viajar_en_el_tiempo):
    _, prestamo = prestar(datos)
    assert datos("GET", "/prestamos/vencidos")[1]["total"] == 0
    viajar_en_el_tiempo(dias=14)                     # el día límite todavía no está vencido
    assert datos("GET", "/prestamos/vencidos")[1]["total"] == 0
    viajar_en_el_tiempo(dias=16)
    status, cuerpo = datos("GET", "/prestamos/vencidos")
    assert status == 200 and cuerpo["total"] == 1
    assert cuerpo["prestamos"][0]["id"] == prestamo["id"]
    assert cuerpo["prestamos"][0]["dias_retraso"] == 2
    assert cuerpo["prestamos"][0]["multa_estimada"] == 2000


def test_vencidos_no_incluye_devueltos(datos, viajar_en_el_tiempo):
    _, prestamo = prestar(datos)
    datos("PUT", "/prestamos/{id}/devolver", id=prestamo["id"])
    viajar_en_el_tiempo(dias=30)
    assert datos("GET", "/prestamos/vencidos")[1]["total"] == 0


def test_ruta_vencidos_no_se_confunde_con_id(datos):
    assert datos("GET", "/prestamos/vencidos")[0] == 200
    assert datos("GET", "/prestamos/{id}", id="vencidos")[0] == 404


# ── PUT /prestamos/{id}/renovar ───────────────────────────────────────

def test_renovar_extiende_14_dias(datos, tabla_dynamodb, redis_fake):
    from datetime import date, timedelta
    _, prestamo = prestar(datos)
    datos("GET", "/usuarios/{id}/prestamos", id="ana")
    status, cuerpo = datos("PUT", "/prestamos/{id}/renovar", id=prestamo["id"])
    assert status == 200 and cuerpo["renovaciones"] == 1
    esperada = date.fromisoformat(prestamo["fecha_limite"]) + timedelta(days=14)
    assert cuerpo["fecha_limite"] == esperada.isoformat()
    item = tabla_dynamodb.query(IndexName="GSI1", KeyConditionExpression="GSI1PK = :g",
                                ExpressionAttributeValues={":g": f"PRESTAMO#{prestamo['id']}"})["Items"][0]
    assert item["GSI3SK"].startswith(esperada.isoformat())
    assert redis_fake.get(cache.cache_key_prestamos_usuario("ana")) is None


def test_renovar_maximo_dos_veces(datos):
    _, prestamo = prestar(datos)
    assert datos("PUT", "/prestamos/{id}/renovar", id=prestamo["id"])[0] == 200
    assert datos("PUT", "/prestamos/{id}/renovar", id=prestamo["id"])[0] == 200
    status, cuerpo = datos("PUT", "/prestamos/{id}/renovar", id=prestamo["id"])
    assert status == 409 and "2 veces" in cuerpo["error"]


def test_renovar_vencido_409(datos, viajar_en_el_tiempo):
    _, prestamo = prestar(datos)
    viajar_en_el_tiempo(dias=20)
    status, cuerpo = datos("PUT", "/prestamos/{id}/renovar", id=prestamo["id"])
    assert status == 409 and "vencido" in cuerpo["error"]


def test_renovar_con_reservas_409(datos):
    _, prestamo = prestar(datos)        # el único ejemplar queda prestado
    datos("POST", "/usuarios", body={"id": "beto", "nombre": "Beto", "email": "b@unimag.edu.co"})
    datos("POST", "/reservas", body={"usuario_id": "beto", "item_id": "cien-anios"})
    status, cuerpo = datos("PUT", "/prestamos/{id}/renovar", id=prestamo["id"])
    assert status == 409 and "reservas" in cuerpo["error"]


def test_renovar_devuelto_409_e_inexistente_404(datos):
    _, prestamo = prestar(datos)
    datos("PUT", "/prestamos/{id}/devolver", id=prestamo["id"])
    assert datos("PUT", "/prestamos/{id}/renovar", id=prestamo["id"])[0] == 409
    assert datos("PUT", "/prestamos/{id}/renovar", id="nope")[0] == 404
