# Documento de diseño — Biblioteca serverless

**Asignatura:** Bases de Datos NoSQL — Universidad del Magdalena
**Integrantes:** Carlos Alfredo Ibañez Ortiz y Adriana Carolina Olivares Vásquez

> *En NoSQL no se diseñan tablas, se diseñan patrones de acceso.*

## 1. Dominio

La biblioteca de la universidad: **libros** (los *ítems* del API `/items`),
**categorías**, **usuarios**, **préstamos**, **reservas** de libros agotados y
**multas**. Los libros se consultan y buscan muchas más veces de las que se
registran o modifican: es una carga de **lectura intensiva**, ideal para un
caché. Los préstamos, devoluciones, reservas y multas cambian contadores en
varios registros a la vez, por eso se escriben con **transacciones**.

## 2. Patrones de acceso (Access Patterns)

Se definieron **antes** de diseñar la tabla. Ninguno usa `Scan`.

| # | Patrón de acceso | Endpoint | Operación DynamoDB / Redis |
|---|---|---|---|
| AP1 | Salud del servicio | `GET /health` | `DescribeTable` + `PING` |
| AP2 | Registrar un libro | `POST /items` | `TransactWriteItems` (libro + contador de categoría) + `SETEX` |
| AP3 | Listar el catálogo por título (paginado) | `GET /items` | `Query` GSI2 `TIPO#LIBRO` |
| AP4 | Libros de un autor, por año | `GET /items?autor=` | `Query` GSI1 `AUTOR#<autor>` |
| AP5 | Buscar libros cuyo título contiene un texto | `GET /items/buscar?q=` | Redis `GET` → `Query` GSI2 + `contains()` → `SETEX` + `SADD` |
| AP6 | Libros más consultados / más prestados | `GET /items/populares` | `ZREVRANGE … WITHSCORES` |
| AP7 | Consultar un libro por id | `GET /items/{id}` | Redis `GET` → `GetItem` → `SETEX` |
| AP8 | Actualizar un libro | `PUT /items/{id}` | `TransactWriteItems` (`UpdateItem` + contadores de categoría) + `SETEX` |
| AP9 | Retirar un libro (sin préstamos ni reservas) | `DELETE /items/{id}` | `TransactWriteItems` (`DeleteItem` + contador) + `DEL` |
| AP10 | Listar categorías | `GET /categorias` | Redis `SMEMBERS` → `Query` GSI2 `TIPO#CATEGORIA` → `SADD` |
| AP11 | Libros de una categoría, por título | `GET /categorias/{nombre}/items` | `Query` GSI3 `CATEGORIA#<nombre>` |
| AP12 | Registrar un usuario | `POST /usuarios` | `PutItem` condicional + `SETEX` |
| AP13 | Listar usuarios por nombre (paginado) | `GET /usuarios` | `Query` GSI2 `TIPO#USUARIO` |
| AP14 | Consultar un usuario | `GET /usuarios/{id}` | Redis `GET` → `GetItem` → `SETEX` |
| AP15 | Actualizar un usuario | `PUT /usuarios/{id}` | `UpdateItem … ReturnValues=ALL_NEW` + `SETEX` |
| AP16 | Eliminar un usuario y su historial | `DELETE /usuarios/{id}` | `Query` PK + `DeleteItem` condicional + `BatchWriteItem` + `DEL` |
| AP17 | Préstamos de un usuario (recientes primero) | `GET /usuarios/{id}/prestamos` | Redis `GET` → `Query` `PK=USER#<id>, begins_with(SK,'LOAN#')` |
| AP18 | Multas de un usuario | `GET /usuarios/{id}/multas` | Redis `GET` → `Query` `PK=USER#<id>, begins_with(SK,'FINE#')` |
| AP19 | Prestar un libro | `POST /prestamos` | `TransactWriteItems` (libro, usuario, préstamo y reserva) + `DEL` + `LREM` + `ZINCRBY` |
| AP20 | Préstamos por estado, por fecha límite | `GET /prestamos?estado=` | `Query` GSI3 `PRESTAMOS#<ESTADO>` |
| AP21 | Préstamos activos vencidos | `GET /prestamos/vencidos` | `Query` GSI3 `PRESTAMOS#ACTIVO AND GSI3SK < hoy` |
| AP22 | Consultar un préstamo por id | `GET /prestamos/{id}` | `Query` GSI1 `PRESTAMO#<id>` |
| AP23 | Devolver un libro (multa si hay retraso) | `PUT /prestamos/{id}/devolver` | `TransactWriteItems` (préstamo, usuario, libro y multa) + `DEL` |
| AP24 | Renovar un préstamo | `PUT /prestamos/{id}/renovar` | `TransactWriteItems` (`ConditionCheck` libro + `UpdateItem` préstamo) + `DEL` |
| AP25 | Reservar un libro agotado | `POST /reservas` | `TransactWriteItems` (libro, `ConditionCheck` usuario, reserva) + `RPUSH` |
| AP26 | Cancelar una reserva | `DELETE /reservas/{id}` | `Query` GSI1 + `TransactWriteItems` + `LREM` |
| AP27 | Fila de espera de un libro | `GET /items/{id}/reservas` | Redis `LRANGE` → `Query` GSI3 + perfiles; si Redis no responde, solo GSI3 |
| AP28 | Registrar una multa | `POST /multas` | `TransactWriteItems` (multa + saldo del usuario) + `DEL` |
| AP29 | Estado del caché | `GET /cache/stats` | `HGETALL`, `DBSIZE`, `SCAN` |

## 3. Modelo de la tabla (Single Table Design)

Una sola tabla (`AppTable`, creada por CDK) con claves genéricas `PK` y `SK`
(String), modo **On-Demand** (`PAY_PER_REQUEST`) y **tres índices globales**
con claves genéricas **sobrecargadas** (cada entidad las usa para un patrón
distinto).

| Entidad | PK | SK | GSI1PK / GSI1SK | GSI2PK / GSI2SK | GSI3PK / GSI3SK |
|---|---|---|---|---|---|
| Libro | `ITEM#<id>` | `METADATA` | `AUTOR#<autor>` / `ANIO#<aaaa>#ITEM#<id>` | `TIPO#LIBRO` / `TITULO#<titulo>#<id>` | `CATEGORIA#<cat>` / `TITULO#<titulo>#<id>` |
| Categoría | `CATEGORIA#<nombre>` | `METADATA` | — | `TIPO#CATEGORIA` / `CATEGORIA#<nombre>` | — |
| Usuario | `USER#<id>` | `PROFILE` | — | `TIPO#USUARIO` / `NOMBRE#<nombre>#<id>` | — |
| Préstamo | `USER#<usuarioId>` | `LOAN#<fecha>#<itemId>#<prestamoId>` | `PRESTAMO#<id>` / `METADATA` | — | `PRESTAMOS#<ACTIVO\|DEVUELTO>` / `<fecha_limite>#<id>` |
| Reserva | `USER#<usuarioId>` | `RESV#<itemId>` | `RESERVA#<id>` / `METADATA` | — | `RESERVAS#ITEM#<itemId>` / `<fecha>#<id>` |
| Multa | `USER#<usuarioId>` | `FINE#<fecha>#<multaId>` | `MULTA#<id>` / `METADATA` | — | — |

Resumen de índices:

| Índice | Para qué se usa |
|---|---|
| Tabla base | Libro, categoría o usuario por id; **colección del usuario** (perfil, préstamos, reservas y multas en la misma partición `USER#<id>`) |
| GSI1 | Libros por autor; préstamo, reserva o multa por su id |
| GSI2 | Listados por tipo: catálogo por título, usuarios por nombre, categorías |
| GSI3 | Libros por categoría; préstamos por estado ordenados por fecha límite (y vencidos); fila de reservas de un libro en orden de llegada |

Ejemplo de datos:

| PK | SK | Atributos principales |
|---|---|---|
| `ITEM#cien-anios` | `METADATA` | titulo, autor, categoria=Novela, ejemplares=2, disponibles=0, prestados=2, reservas_pendientes=1 |
| `CATEGORIA#NOVELA` | `METADATA` | nombre=Novela, libros=5 |
| `USER#ana` | `PROFILE` | nombre, email, prestamos_activos=1, saldo_multas=3000 |
| `USER#ana` | `LOAN#2026-11-10T20:00:00+00:00#cien-anios#9f3a…` | estado=ACTIVO, fecha_limite=2026-11-24, renovaciones=0 |
| `USER#beto` | `RESV#cien-anios` | estado=PENDIENTE, fecha_reserva |
| `USER#ana` | `FINE#2026-11-28T15:00:00+00:00#7c1e…` | monto=3000, origen=AUTOMATICA, estado=PENDIENTE |

Decisiones:

- **Colección de ítems por usuario**: perfil, préstamos, reservas y multas
  comparten la partición `USER#<id>`. "Préstamos de un usuario" o "multas de
  un usuario" son una sola `Query` con `begins_with(SK, …)`, y como la `SK`
  empieza con la fecha ISO salen ordenados (`ScanIndexForward=false` → más
  recientes primero). Es el reemplazo NoSQL de un `JOIN`.
- **Una reserva por usuario y libro**: `SK=RESV#<itemId>` hace que una segunda
  reserva del mismo libro choque con `attribute_not_exists(PK)` (409).
- **Préstamos por estado y vencidos (GSI3)**: `GSI3SK` empieza con la fecha
  límite, así que `GSI3SK < '2026-11-10'` devuelve exactamente los préstamos
  activos ya vencidos, con una `Query` por rango. Al devolver, `GSI3PK` pasa
  de `PRESTAMOS#ACTIVO` a `PRESTAMOS#DEVUELTO`; al renovar se reescribe
  `GSI3SK` con la nueva fecha.
- **Fila de reservas (GSI3)**: `GSI3SK=<fecha con microsegundos>#<id>` deja
  las reservas de un libro en orden de llegada; es la copia durable de la
  List de Redis.
- **Contadores desnormalizados**: el libro guarda `disponibles`, `prestados`
  y `reservas_pendientes`; el usuario `prestamos_activos` y `saldo_multas`; la
  categoría `libros`. Permiten validar reglas con **condiciones** en vez de
  contar registros, y se actualizan en la misma transacción que el cambio.
- **Hot partitions**: las particiones de GSI2 (`TIPO#LIBRO`) y GSI3
  (`PRESTAMOS#ACTIVO`) concentran muchos ítems. Para el volumen de una
  biblioteca universitaria es aceptable; a gran escala se repartirían con
  *write sharding* (por ejemplo `PRESTAMOS#ACTIVO#<0-9>`).
- **Búsqueda por texto**: DynamoDB no tiene búsqueda "contiene" indexada. Se
  guarda `titulo_busqueda` (mayúsculas, sin tildes) y se hace `Query` sobre
  la partición `TIPO#LIBRO` del GSI2 con `FilterExpression contains()`; esa
  lectura recorre el catálogo, por eso **el resultado se cachea** en Redis. En
  producción se usaría un motor de búsqueda (OpenSearch) alimentado por
  DynamoDB Streams.

### Reglas de negocio

| Regla | Dónde se garantiza |
|---|---|
| No prestar sin ejemplares (`disponibles > 0`) | Condición del `UpdateItem` del libro en la transacción |
| Máximo 3 préstamos activos por usuario | Condición `prestamos_activos < :max` |
| No prestar si el usuario tiene multas pendientes (`saldo_multas > 0`) | Condición `saldo_multas <= :cero` en la transacción de `POST /prestamos` (409) |
| Una multa manual solo se registra si el usuario ha tenido al menos un préstamo | Lectura del historial `LOAN#` y `ConditionCheck` del préstamo en `POST /multas` (409) |
| Monto de una multa manual: entre $1.000 y $500.000, en múltiplos de $1.000 | Validación de `POST /multas` (400) |
| Plazo de 14 días (`DIAS_PRESTAMO`) | `fecha_limite` al prestar |
| Solo se reserva un libro **sin ejemplares libres** | Condición `disponibles = 0 OR reservas_pendientes > 0` |
| Si hay fila, el libro solo se presta a los **N primeros** de la fila (N = ejemplares disponibles); el préstamo **consume** la reserva | `prestamos.crear` lee la fila y agrega a la transacción el `Delete` de la reserva y `reservas_pendientes - 1` |
| Renovar: solo préstamos activos, **no vencidos**, máximo **2** veces y solo si el libro **no tiene reservas** (alguien lo espera) | Validación previa con mensaje claro + condiciones en la transacción (`ConditionCheck` del libro) |
| Cada renovación suma 14 días a la fecha límite vigente | `UpdateItem` del préstamo |
| Devolución tardía → multa automática de `MULTA_POR_DIA` (1000) × días de retraso | `Put` de la multa y suma a `saldo_multas` dentro de la transacción de devolución |
| No borrar libros con préstamos o reservas, ni usuarios con préstamos o reservas | Condiciones del `DeleteItem` y validación previa (409) |

Las multas quedan registradas con estado `PENDIENTE` y se suman a
`saldo_multas`; el pago de multas queda por fuera del alcance de este API.
Mientras el saldo sea mayor que cero, `POST /prestamos` responde `409`.
`POST /multas` (multa manual) responde `409` si el usuario nunca ha tenido
un préstamo, aunque `prestamo_id` sea opcional; si se envía, debe ser un
préstamo válido de ese usuario.

## 4. Redis: estructuras, patrones y justificación

DynamoDB es la **fuente de verdad**; Redis es la **velocidad**.

| Clave | Tipo | Contenido | TTL |
|---|---|---|---|
| `item#<id>` | String | JSON del libro | 300 s |
| `usuario#<id>` | String | JSON del usuario | 300 s |
| `usuario#<id>#prestamos` | String | JSON con los préstamos del usuario | 300 s |
| `usuario#<id>#multas` | String | JSON con las multas del usuario | 300 s |
| `busqueda:<texto>` | String | Resultado de `GET /items/buscar` | 300 s |
| `indice:busquedas` | **Set** | Claves `busqueda:*` vigentes (para invalidarlas sin `KEYS`) | 300 s |
| `categorias` | **Set** | Nombres de las categorías con libros | 300 s |
| `cola:item#<id>` | **List** | Ids de reserva del libro en orden de llegada | — |
| `ranking:items:consultas` | **Sorted Set** | id del libro → número de consultas | — |
| `ranking:items:prestamos` | **Sorted Set** | id del libro → número de préstamos | — |
| `stats:cache` | **Hash** | `hits`, `misses` | — |

| Patrón | Dónde | Justificación |
|---|---|---|
| **Cache-Aside (Lazy Loading)** | `GET /items/{id}`, `/items/buscar`, `/categorias`, `/usuarios/{id}`, `/usuarios/{id}/prestamos`, `/usuarios/{id}/multas` | Lecturas frecuentes; solo se cachea lo que realmente se consulta. La búsqueda es la lectura más costosa en DynamoDB, la que más gana. |
| **Write-Through** | `POST /items`, `PUT /items/{id}`, `POST /usuarios`, `PUT /usuarios/{id}` | El dato recién creado o editado se consulta enseguida: el primer `GET` ya es *hit* y nunca hay versión vieja en caché. |
| **TTL por clave** | Todas las claves String y los Sets | `SETEX` (= `SET` + `EXPIRE 300`): aunque algo cambie por fuera del API, en 5 minutos como máximo el caché se renueva. |
| **Invalidación** | Préstamos, devoluciones, renovaciones, reservas, multas, borrados; cambios de libros (búsquedas y categorías) | Un préstamo cambia el stock del libro y los datos del usuario: se borran `item#<id>`, `usuario#<id>` y `usuario#<id>#prestamos`. Crear, editar o borrar un libro invalida **todas** las búsquedas (`SMEMBERS indice:busquedas` + `DEL`) y el Set `categorias`. |
| **Cola (List)** | `POST /reservas` (`RPUSH`), `DELETE /reservas/{id}` y préstamo de un reservado (`LREM`) | FIFO natural: la posición en la fila es el índice en la List. Si Redis se reinicia o la List no coincide con `reservas_pendientes`, se reconstruye desde el GSI3 (`DEL` + `RPUSH` en un `MULTI`). |
| **Set** | `categorias`, `indice:busquedas` | Conjuntos sin repetidos; `SMEMBERS` en una sola operación. |
| **Contadores en tiempo real (Sorted Set)** | `GET /items/{id}`, `POST /prestamos` → `GET /items/populares` | Rankings sin escribir en DynamoDB en cada lectura. |
| **Métricas (Hash)** | `GET /cache/stats` | Demuestra con datos la tasa de aciertos (*hit ratio*). Se usa `SCAN`, no `KEYS`. |

**Qué no se cachea:** los listados paginados (`GET /items`, `/usuarios`,
`/prestamos`, `/prestamos/vencidos`, `/categorias/{nombre}/items`) cambian
con cada alta, préstamo o devolución y dependen del cursor; `GET
/prestamos/{id}` se consulta poco. Los resultados de búsqueda guardan solo
`id`, `titulo`, `autor`, `anio` y `categoria` (no el stock), así que los
préstamos no necesitan invalidarlos.

**Modo degradado:** todas las operaciones de Redis están envueltas en
`try/except redis.RedisError` con timeouts de 2 s. Si Redis no responde, el
API sigue funcionando solo con DynamoDB (`"_fuente": "dynamodb"`), la fila de
reservas se arma desde el GSI3 y `/health` responde `503` "degradado". Solo
los endpoints que viven únicamente en Redis (`/items/populares` y
`/cache/stats`) responden `503`.

## 5. Arquitectura

```
Cliente (curl / Postman / frontend) ──HTTP :4566──▶ API Gateway (emulador)
                                                     │
                     ┌───────────────────────────────┴───────────┐
                     ▼                                           ▼
     Lambda ItemsHandler (Python 3.11)                 Lambda HealthHandler
     27 rutas: libros · categorías · usuarios ·        (Python 3.11)
     préstamos · reservas · multas · caché
        │                     │                          │          │
  redis-py :6379      boto3 (AWS_ENDPOINT_URL)         PING   DescribeTable
        ▼                     ▼                          ▼          ▼
      Redis      DynamoDB AppTable (+GSI1, GSI2, GSI3)  Redis     DynamoDB
```

Una sola Lambda atiende las rutas del dominio: comparte los clientes de Redis
(`cache.py`) y DynamoDB (`comun.py`) y enruta por el **recurso** de API
Gateway (`handler.py`). Como API Gateway ya distingue el recurso fijo
`/items/buscar` del recurso `/items/{id}` (y `/prestamos/vencidos` de
`/prestamos/{id}`), el enrutador es un diccionario `(método, recurso)` y el
orden de declaración no importa. `GET /items/{id}/reservas` es un recurso
hijo de `/items/{id}`: devuelve la fila ordenada (LRANGE, o GSI3 si Redis
no responde) con el nombre de cada persona. El código está separado por entidad
(`libros.py`, `categorias.py`, `usuarios.py`, `prestamos.py`, `reservas.py`,
`multas.py`). La infraestructura se define con **AWS CDK en Python**
(`app/infra/stack.py`).
