"""
ProyectoFinalStack
──────────────────
Define la infraestructura serverless del proyecto final:
  - Tabla DynamoDB  (Single Table Design, PK+SK)
  - Lambda Layer    con las dependencias de las Lambdas (boto3, botocore, redis)
  - Lambda ItemsHandler: biblioteca (libros, categorías, usuarios,
                    préstamos, reservas, multas y estadísticas del caché) con Cache-Aside,
                    Write-Through, TTL por ítem e invalidación en Redis
  - Lambda /health  (GET) health check del servicio
  - API Gateway     (RestApi) que enruta las peticiones
"""
import os
import shutil
import subprocess
import sys
from typing import Any

import aws_cdk as cdk
import jsii
from aws_cdk import (
    Stack,
    aws_dynamodb as ddb,
    aws_lambda as _lambda,
    aws_apigateway as apigw,
    RemovalPolicy,
    CfnOutput,
)
from constructs import Construct

# Runtime común a todas las Lambdas y a la layer
RUNTIME: _lambda.Runtime = _lambda.Runtime.PYTHON_3_11
DIR_LAYER: str = "layers/dependencias"
REQUIREMENTS_LAYER: str = os.path.join(DIR_LAYER, "requirements.txt")

# LocalStack Community acepta las Lambda Layers pero no las monta al ejecutar
# (es una función de LocalStack Pro). Cuando se despliega en el emulador
# (cdk-dev define AWS_ENDPOINT_URL), las dependencias se empaquetan dentro
# del código de cada Lambda en lugar de usar la layer.
ES_LOCAL: bool = "AWS_ENDPOINT_URL" in os.environ


def _pip_install(requirements: str, destino: str) -> None:
    """Instala las dependencias en `destino` para el runtime de Lambda."""
    subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "--quiet",
            "--requirement", requirements,
            "--target", destino,
            # Descargar wheels para el sistema y la versión de Python
            # de Lambda, no los del contenedor de desarrollo
            "--platform", "manylinux2014_x86_64",
            "--implementation", "cp",
            "--python-version", "3.11",
            "--only-binary=:all:",
            # Sin .pyc: hace el resultado reproducible (mismo hash de asset
            # si no cambian las dependencias → no se redespliega sin motivo)
            "--no-compile",
        ],
        check=True,
    )


@jsii.implements(cdk.ILocalBundling)
class _PipLocal:
    """
    Empaqueta con el pip del propio contenedor cdk-dev, sin necesitar Docker.

    - Layer:  instala en "python/", donde Lambda busca las librerías de una layer.
    - Lambda: copia el código fuente y las dependencias juntas en la raíz.
    """

    def __init__(
        self,
        requirements: str,
        subdir: str = "",
        copiar_desde: str | None = None,
    ) -> None:
        self.requirements: str = requirements
        self.subdir: str = subdir
        self.copiar_desde: str | None = copiar_desde

    def try_bundle(self, output_dir: str, *args: Any, **kwargs: Any) -> bool:
        if self.copiar_desde:
            shutil.copytree(
                self.copiar_desde,
                output_dir,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
        _pip_install(self.requirements, os.path.join(output_dir, self.subdir))
        return True


def _codigo_lambda(directorio: str) -> _lambda.Code:
    """
    Código de una Lambda. En AWS solo nuestros .py (las dependencias van en
    la layer); en LocalStack, nuestros .py + las dependencias de la layer.
    """
    if not ES_LOCAL:
        return _lambda.Code.from_asset(directorio)
    return _lambda.Code.from_asset(
        directorio,
        # El hash se calcula sobre el resultado, así también cambia
        # cuando cambian las dependencias y no solo el código
        asset_hash_type=cdk.AssetHashType.OUTPUT,
        bundling=cdk.BundlingOptions(
            # Solo se usaría si falla el bundling local (cdk-dev no tiene Docker)
            image=RUNTIME.bundling_image,
            command=["bash", "-c", "echo 'Requiere bundling local con pip' && exit 1"],
            local=_PipLocal(REQUIREMENTS_LAYER, copiar_desde=directorio),
        ),
    )


class ProyectoFinalStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs: Any) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── 1. TABLA DYNAMODB ─────────────────────────────────────────
        # Single Table Design: PK y SK genéricos para soportar
        # múltiples tipos de entidades en una sola tabla.
        tabla: ddb.Table = ddb.Table(
            self,
            "AppTable",
            partition_key=ddb.Attribute(
                name="PK",
                type=ddb.AttributeType.STRING,
            ),
            sort_key=ddb.Attribute(
                name="SK",
                type=ddb.AttributeType.STRING,
            ),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            # DESTROY permite borrar la tabla al hacer `cdklocal destroy`
            # En producción usar RETAIN
            removal_policy=RemovalPolicy.DESTROY,
        )

        # GSI1: "libros de un autor" (Query por GSI1PK=AUTOR#<autor>,
        # ordenados por GSI1SK=ANIO#<anio>#ITEM#<id>). Evita usar Scan.
        tabla.add_global_secondary_index(
            index_name="GSI1",
            partition_key=ddb.Attribute(name="GSI1PK", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="GSI1SK", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.ALL,
        )
        # GSI1 también se sobrecarga para buscar por id: PRESTAMO#<id>,
        # RESERVA#<id> y MULTA#<id>.

        # GSI2: listados por tipo de entidad (TIPO#LIBRO por título,
        # TIPO#USUARIO por nombre, TIPO#CATEGORIA). Query, no Scan.
        tabla.add_global_secondary_index(
            index_name="GSI2",
            partition_key=ddb.Attribute(name="GSI2PK", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="GSI2SK", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.ALL,
        )

        # GSI3 (sobrecargado): libros por categoría (CATEGORIA#<nombre>),
        # préstamos por estado ordenados por fecha límite (PRESTAMOS#ACTIVO)
        # y fila de reservas de un libro (RESERVAS#ITEM#<id>).
        tabla.add_global_secondary_index(
            index_name="GSI3",
            partition_key=ddb.Attribute(name="GSI3PK", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="GSI3SK", type=ddb.AttributeType.STRING),
            projection_type=ddb.ProjectionType.ALL,
        )

        # Variables de entorno comunes para todas las Lambdas
        env_comun: dict[str, str] = {
            "TABLE_NAME": tabla.table_name,
            "REDIS_HOST": os.environ.get("REDIS_HOST", "redis"),
            "REDIS_PORT": os.environ.get("REDIS_PORT", "6379"),
            "REDIS_TTL":  "300",   # segundos de vida en caché
            "MAX_PRESTAMOS": "3",     # préstamos activos por usuario
            "DIAS_PRESTAMO": "14",    # días para devolver (y de cada renovación)
            "MAX_RENOVACIONES": "2",  # renovaciones por préstamo
            "MULTA_POR_DIA": "1000",  # multa automática por día de retraso
        }

        # ── 2. LAMBDA LAYER (dependencias) ────────────────────────────
        # Code.from_asset("lambdas/...") solo empaqueta nuestros .py.
        # Las librerías externas van en una layer compartida, construida
        # con pip a partir de layers/dependencias/requirements.txt.
        # Se reconstruye solo cuando cambia ese archivo.
        # En LocalStack no se crea: no la aplicaría (ver ES_LOCAL).
        layers: list[_lambda.ILayerVersion] = []
        if not ES_LOCAL:
            layer_dependencias: _lambda.LayerVersion = _lambda.LayerVersion(
                self,
                "DependenciasLayer",
                code=_lambda.Code.from_asset(
                    DIR_LAYER,
                    bundling=cdk.BundlingOptions(
                        # image/command solo se usan si falla el bundling local
                        image=RUNTIME.bundling_image,
                        command=[
                            "bash", "-c",
                            "pip install -r requirements.txt -t /asset-output/python",
                        ],
                        local=_PipLocal(REQUIREMENTS_LAYER, subdir="python"),
                    ),
                ),
                compatible_runtimes=[RUNTIME],
                description="boto3, botocore y redis para las Lambdas",
            )
            layers.append(layer_dependencias)

        # ── 3. LAMBDA /items ──────────────────────────────────────────
        # Atiende todas las rutas del dominio (libros, categorías, usuarios,
        # préstamos, reservas, multas y /cache/stats). Cache-Aside: consulta Redis antes de DynamoDB.
        lambda_items: _lambda.Function = _lambda.Function(
            self,
            "ItemsHandler",
            runtime=RUNTIME,
            handler="handler.main",
            code=_codigo_lambda("lambdas/items"),
            layers=layers,
            environment=env_comun,
            timeout=cdk.Duration.seconds(10),
        )
        tabla.grant_read_write_data(lambda_items)

        # ── 4. LAMBDA /health ─────────────────────────────────────────
        # Health check: verifica conectividad con DynamoDB y Redis
        lambda_health: _lambda.Function = _lambda.Function(
            self,
            "HealthHandler",
            runtime=RUNTIME,
            handler="handler.main",
            code=_codigo_lambda("lambdas/health"),
            layers=layers,
            environment=env_comun,
            timeout=cdk.Duration.seconds(5),
        )
        tabla.grant_read_data(lambda_health)

        # ── 5. API GATEWAY ────────────────────────────────────────────
        # RestApi con integración proxy (LambdaIntegration) en cada ruta:
        # la Lambda recibe el método, el recurso y los parámetros.
        api: apigw.RestApi = apigw.RestApi(
            self,
            "Api",
            rest_api_name="ProyectoFinal-Biblioteca-API",
            description="Catálogo de biblioteca — DynamoDB + Redis + CDK",
            deploy_options=apigw.StageOptions(stage_name="prod"),
            # CORS para el frontend de demo (frontend/index.html)
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                allow_headers=["Content-Type"],
            ),
        )

        integracion_items = apigw.LambdaIntegration(lambda_items)

        def rutas(recurso: apigw.IResource, *metodos: str) -> None:
            for metodo in metodos:
                recurso.add_method(metodo, integracion_items)

        # Libros
        r_items = api.root.add_resource("items")                     # /items
        rutas(r_items, "POST", "GET")
        rutas(r_items.add_resource("buscar"), "GET")                 # /items/buscar
        rutas(r_items.add_resource("populares"), "GET")              # /items/populares
        r_item_id = r_items.add_resource("{id}")                     # /items/{id}
        rutas(r_item_id, "GET", "PUT", "DELETE")
        rutas(r_item_id.add_resource("reservas"), "GET")             # /items/{id}/reservas

        # Categorías
        r_categorias = api.root.add_resource("categorias")           # /categorias
        rutas(r_categorias, "GET")
        rutas(r_categorias.add_resource("{nombre}").add_resource("items"), "GET")

        # Usuarios
        r_usuarios = api.root.add_resource("usuarios")               # /usuarios
        rutas(r_usuarios, "POST", "GET")
        r_usuario_id = r_usuarios.add_resource("{id}")               # /usuarios/{id}
        rutas(r_usuario_id, "GET", "PUT", "DELETE")
        rutas(r_usuario_id.add_resource("prestamos"), "GET")         # /usuarios/{id}/prestamos
        rutas(r_usuario_id.add_resource("multas"), "GET")            # /usuarios/{id}/multas

        # Préstamos
        r_prestamos = api.root.add_resource("prestamos")             # /prestamos
        rutas(r_prestamos, "POST", "GET")
        rutas(r_prestamos.add_resource("vencidos"), "GET")           # /prestamos/vencidos
        r_prestamo_id = r_prestamos.add_resource("{id}")             # /prestamos/{id}
        rutas(r_prestamo_id, "GET")
        rutas(r_prestamo_id.add_resource("devolver"), "PUT")         # /prestamos/{id}/devolver
        rutas(r_prestamo_id.add_resource("renovar"), "PUT")          # /prestamos/{id}/renovar

        # Reservas
        r_reservas = api.root.add_resource("reservas")               # /reservas
        rutas(r_reservas, "POST")
        rutas(r_reservas.add_resource("{id}"), "DELETE")             # /reservas/{id}

        # Multas
        rutas(api.root.add_resource("multas"), "POST")               # /multas

        # Caché
        rutas(api.root.add_resource("cache").add_resource("stats"), "GET")  # /cache/stats

        # /health → GET (Lambda propia)
        api.root.add_resource("health").add_method(
            "GET",
            apigw.LambdaIntegration(lambda_health),
        )

        # ── 6. OUTPUTS ────────────────────────────────────────────────
        CfnOutput(self, "ApiUrl",
                  value=api.url,
                  description="URL base del API Gateway")
        CfnOutput(self, "TableName",
                  value=tabla.table_name,
                  description="Nombre de la tabla DynamoDB")
