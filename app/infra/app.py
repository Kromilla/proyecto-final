#!/usr/bin/env python3
"""
Punto de entrada del CDK App.
Ejecutar con: cdklocal deploy  (dentro del contenedor cdk-dev)
"""
import aws_cdk as cdk
from stack import ProyectoFinalStack

app: cdk.App = cdk.App()

ProyectoFinalStack(
    app,
    "ProyectoFinalStack",
    env=cdk.Environment(
        account="000000000000",   # cuenta dummy para el emulador
        region="us-east-1",
    ),
)

app.synth()
