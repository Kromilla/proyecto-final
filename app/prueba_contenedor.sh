#!/bin/bash
# Corre probar_api.sh dentro de cdk-dev (Linux, UTF-8)
export LANG=C.UTF-8 LC_ALL=C.UTF-8
cd /app
echo "== test UTF-8 =="; grep -c no_utf8 tests/test_items.py; python -m pytest tests/ -q 2>&1 | tail -1
command -v curl >/dev/null || echo "NO HAY CURL"
API=$(python -c "import boto3;c=boto3.client('apigateway',endpoint_url='http://emulador:4566',region_name='us-east-1',aws_access_key_id='test',aws_secret_access_key='test');print(' '.join(a['id'] for a in c.get_rest_apis()['items']))")
echo "APIs: $API"; API=${API%% *}
sed -e 's#^redis() .*#redis() { python -c "import redis,sys;print(redis.Redis(\x27redis\x27,decode_responses=True).execute_command(*sys.argv[1:]))" "$@"; }#' \
    -e 's#python3 #python #g' -e 's/set -euo pipefail/set -uo pipefail/' /app/probar_api.sh > /tmp/p.sh
bash /tmp/p.sh "http://emulador:4566/restapis/$API/prod/_user_request_" 2>&1
