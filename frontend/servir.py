"""Sirve el frontend y GET /detectar-api.

El navegador no puede listar :4566/restapis (403 por Origin). Esta ruta
lo consulta desde el servidor y devuelve la URL del API.
"""
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.request import Request, urlopen

PUERTO = 8765
EMULADOR = "http://127.0.0.1:4566"
# LocalStack enruta el servicio según el nombre que aparece en la credencial.
AUTH = (
    "AWS4-HMAC-SHA256 Credential=test/20261112/us-east-1/apigateway/aws4_request, "
    "SignedHeaders=host;x-amz-date, Signature=local"
)


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/detectar-api":
            self._detectar()
            return
        super().do_GET()

    def _detectar(self):
        try:
            req = Request(
                f"{EMULADOR}/restapis",
                headers={"Authorization": AUTH, "Accept": "application/json"},
            )
            with urlopen(req, timeout=4) as respuesta:
                datos = json.loads(respuesta.read().decode())
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as error:
            self._json({"url": None, "error": str(error)})
            return
        apis = datos.get("item") or datos.get("items") or []
        biblioteca = [a for a in apis if "biblioteca" in (a.get("name") or "").lower()]
        biblioteca.sort(key=lambda a: a.get("createdDate") or 0, reverse=True)
        if not biblioteca:
            self._json({"url": None})
            return
        host = (self.headers.get("Host") or "127.0.0.1").split(":")[0]
        elegida = biblioteca[0]
        url = f"http://{host}:4566/restapis/{elegida['id']}/prod/_user_request_"
        self._json({"url": url, "id": elegida["id"], "nombre": elegida.get("name")})

    def _json(self, obj):
        cuerpo = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)


if __name__ == "__main__":
    ThreadingHTTPServer.allow_reuse_address = True
    servidor = ThreadingHTTPServer(("127.0.0.1", PUERTO), Handler)
    print(f"Frontend en http://127.0.0.1:{PUERTO}/index.html", flush=True)
    servidor.serve_forever()
