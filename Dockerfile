# Imagen del contenedor cdk-dev: Python 3.11 + Node.js (AWS CDK) + cdklocal
FROM python:3.11-slim

# Node.js, npm, curl y git (necesarios para AWS CDK)
RUN apt-get update && apt-get install -y \
    nodejs \
    npm \
    curl \
    git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# AWS CDK y cdklocal (wrapper que redirige el despliegue al emulador :4566)
RUN npm install -g aws-cdk aws-cdk-local

WORKDIR /app

# Dependencias Python (CDK + runtime de las Lambdas + tests).
# app/requirements.txt incluye las de la Lambda Layer con -r.
COPY app/requirements.txt .
COPY app/layers/dependencias/requirements.txt layers/dependencias/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Credenciales dummy: el emulador acepta cualquier valor
ENV AWS_ACCESS_KEY_ID=test
ENV AWS_SECRET_ACCESS_KEY=test
ENV AWS_DEFAULT_REGION=us-east-1

CMD ["bash"]
