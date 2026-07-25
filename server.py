import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from detectevents import detectar
from playmusic import shutdown_audio

latest_payload = None
last_processed_payload = None
payload_lock = threading.Lock()
payload_ready = threading.Event()
http_server = None

DEBUG_GSI_PAYLOAD = True
CONFIGURED_GSI_COMPONENTS = {
    "provider": "provider",
    "map": "map",
    "round": "round",
    "phase_countdowns": "phase_countdowns",
    "player_id": "player",
    "player_state": "player",
    "bomb": "bomb",
    "player_match_stats": "player",
    "player_weapons": "player",
    "allplayers_id": "allplayers",
}


def _store_latest_payload(raw_data):
    global latest_payload

    with payload_lock:
        latest_payload = raw_data
    payload_ready.set()


def _take_latest_payload():
    global latest_payload

    with payload_lock:
        raw_data = latest_payload
        latest_payload = None
        payload_ready.clear()
        return raw_data

def loop_eventos():
    while True:
        payload_ready.wait()
        raw_data = _take_latest_payload()

        if raw_data is None:
            continue

        try:
            data = json.loads(raw_data)
            _debug_payload(data)
            detectar(data)
        except Exception as e:
            print("erro ao processar payload GSI:", e)


def _debug_payload(data):
    if not DEBUG_GSI_PAYLOAD:
        return

    received_keys = set(data.keys())
    print("===== GSI PAYLOAD =====")
    print("data.keys():", list(data.keys()))
    print("Componentes configurados x recebidos:")
    for component, payload_key in CONFIGURED_GSI_COMPONENTS.items():
        status = "OK" if payload_key in received_keys else "AUSENTE"
        print(f"- {component} -> {payload_key}: {status}")
    print("phase_countdowns:", data.get("phase_countdowns"))
    print(json.dumps(data, indent=4, ensure_ascii=False))


class FastHTTPServer(HTTPServer):
    daemon_threads = True


class Server(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_POST(self):
        global http_server

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0

        raw_data = self.rfile.read(length)

        try:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

        try:
            if self.path == "/shutdown":
                if http_server is not None:
                    threading.Thread(target=http_server.shutdown, daemon=True).start()
                return

            _store_latest_payload(raw_data)
        except Exception as e:
            print("erro:", e)


def start_server():
    global http_server

    print("servidor iniciado - esperando eventos do CS2")
    threading.Thread(target=loop_eventos, daemon=True).start()
    http_server = FastHTTPServer(("127.0.0.1", 3000), Server)
    try:
        http_server.serve_forever()
    finally:
        try:
            http_server.server_close()
        except Exception:
            pass
        shutdown_audio()


if __name__ == "__main__":
    start_server()
