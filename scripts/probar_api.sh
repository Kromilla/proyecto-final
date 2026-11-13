#!/usr/bin/env bash
# Recorre los 29 endpoints del API y muestra el efecto en Redis.
# Ejecutar desde la carpeta del proyecto, en el host (usa `docker compose exec redis`).
# Uso:  ./scripts/probar_api.sh <api-id>
#   o:  ./scripts/probar_api.sh <url-base>   (por ejemplo el ApiUrl del deploy)
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Uso: $0 <api-id | url-base>" >&2
  exit 1
fi
if [[ "$1" == http* ]]; then
  BASE="${1%/}"
else
  BASE="http://localhost:4566/restapis/$1/prod/_user_request_"
fi
echo "URL base: $BASE"

JSON=(-H "Content-Type: application/json")
paso() { echo; echo "── $*"; }
redis() { docker compose exec -T redis redis-cli "$@"; }
campo() { python3 -c "import json,sys; print(json.load(sys.stdin)[\"$1\"])"; }

paso "1. GET /health"
curl -s "$BASE/health"; echo

paso "2. POST /items (Write-Through) — dos libros"
curl -s -X POST "${JSON[@]}" \
  -d '{"id":"cien-anios","titulo":"Cien años de soledad","autor":"Gabriel García Márquez","anio":1967,"categoria":"Novela","ejemplares":1}' \
  "$BASE/items"; echo
curl -s -X POST "${JSON[@]}" \
  -d '{"id":"cosmos","titulo":"Cosmos","autor":"Carl Sagan","anio":1980,"categoria":"Ciencia","ejemplares":2}' \
  "$BASE/items"; echo

paso "   Se borra item#cien-anios en Redis para forzar un miss"
redis DEL item#cien-anios

paso "6. GET /items/cien-anios → miss (\"dynamodb\") y luego hit (\"cache\")"
curl -s "$BASE/items/cien-anios"; echo
curl -s "$BASE/items/cien-anios"; echo
redis TTL item#cien-anios

paso "7. PUT /items/cien-anios (Write-Through)"
curl -s -X PUT "${JSON[@]}" -d '{"genero":"Realismo mágico"}' "$BASE/items/cien-anios"; echo

paso "3. GET /items (GSI2) y GET /items?autor= (GSI1)"
curl -s "$BASE/items?limite=10"; echo
curl -s -G "$BASE/items" --data-urlencode "autor=Gabriel García Márquez"; echo

paso "4. GET /items/buscar?q=años (dos veces: dynamodb → cache)"
curl -s -G "$BASE/items/buscar" --data-urlencode "q=años"; echo
curl -s -G "$BASE/items/buscar" --data-urlencode "q=años"; echo
redis SMEMBERS indice:busquedas

paso "9. GET /categorias (Set) y 10. GET /categorias/Novela/items (GSI3)"
curl -s "$BASE/categorias"; echo
redis SMEMBERS categorias
curl -s "$BASE/categorias/Novela/items"; echo

paso "11. POST /usuarios, 12. GET /usuarios, 13. GET /usuarios/ana, 14. PUT /usuarios/ana"
for u in ana beto; do
  curl -s -X POST "${JSON[@]}" -d "{\"id\":\"$u\",\"nombre\":\"${u^}\",\"email\":\"$u@unimag.edu.co\"}" "$BASE/usuarios"; echo
done
curl -s "$BASE/usuarios?limite=10"; echo
curl -s "$BASE/usuarios/ana"; echo
curl -s -X PUT "${JSON[@]}" -d '{"programa":"Ingeniería de Sistemas"}' "$BASE/usuarios/ana"; echo

paso "18. POST /prestamos (ana se lleva el único ejemplar)"
PRESTAMO=$(curl -s -X POST "${JSON[@]}" -d '{"usuario_id":"ana","item_id":"cien-anios"}' "$BASE/prestamos")
echo "$PRESTAMO"
PRESTAMO_ID=$(echo "$PRESTAMO" | campo id)

paso "19. GET /prestamos?estado=activo · 20. GET /prestamos/vencidos · 21. GET /prestamos/$PRESTAMO_ID"
curl -s "$BASE/prestamos?estado=activo"; echo
curl -s "$BASE/prestamos/vencidos"; echo
curl -s "$BASE/prestamos/$PRESTAMO_ID"; echo

paso "23. PUT /prestamos/$PRESTAMO_ID/renovar (+14 días)"
curl -s -X PUT "$BASE/prestamos/$PRESTAMO_ID/renovar"; echo

paso "24. POST /reservas (beto entra a la fila) y la List en Redis"
RESERVA=$(curl -s -X POST "${JSON[@]}" -d '{"usuario_id":"beto","item_id":"cien-anios"}' "$BASE/reservas")
echo "$RESERVA"
RESERVA_ID=$(echo "$RESERVA" | campo id)
redis LRANGE cola:item#cien-anios 0 -1

paso "28. GET /items/cien-anios/reservas (fila con nombres y posición)"
curl -s "$BASE/items/cien-anios/reservas"; echo

paso "   Con reservas pendientes ya no se puede renovar → 409"
curl -s -X PUT "$BASE/prestamos/$PRESTAMO_ID/renovar"; echo

paso "25. DELETE /reservas/$RESERVA_ID (LREM)"
curl -s -X DELETE "$BASE/reservas/$RESERVA_ID"; echo
redis LRANGE cola:item#cien-anios 0 -1

paso "16. GET /usuarios/ana/prestamos"
curl -s "$BASE/usuarios/ana/prestamos"; echo

paso "22. PUT /prestamos/$PRESTAMO_ID/devolver"
curl -s -X PUT "$BASE/prestamos/$PRESTAMO_ID/devolver"; echo

paso "27. POST /multas y 17. GET /usuarios/ana/multas"
MULTA=$(curl -s -X POST "${JSON[@]}" -d '{"usuario_id":"ana","monto":5000,"motivo":"Libro dañado"}' "$BASE/multas")
echo "$MULTA"
curl -s "$BASE/usuarios/ana/multas"; echo

paso "28. POST /multas/{id}/pagar"
MULTA_ID=$(echo "$MULTA" | campo id)
curl -s -X POST "$BASE/multas/$MULTA_ID/pagar"; echo
curl -s "$BASE/usuarios/ana/multas"; echo

paso "5. GET /items/populares (Sorted Sets)"
curl -s "$BASE/items/populares?por=consultas&n=5"; echo
curl -s "$BASE/items/populares?por=prestamos&n=5"; echo

paso "29. GET /cache/stats"
curl -s "$BASE/cache/stats"; echo

paso "   Errores: JSON inválido → 400, libro inexistente → 404"
curl -s -X POST "${JSON[@]}" -d '{malo' "$BASE/items"; echo
curl -s "$BASE/items/no-existe"; echo

paso "8. DELETE /items/cosmos (invalida item#cosmos, búsquedas y categorías)"
curl -s -X DELETE "$BASE/items/cosmos"; echo
redis EXISTS item#cosmos categorias

paso "15. DELETE /usuarios/beto"
curl -s -X DELETE "$BASE/usuarios/beto"; echo
