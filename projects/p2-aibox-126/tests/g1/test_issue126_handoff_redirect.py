"""#126 scoped ingest keys must not leave the configured WeKnora origin."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from implementation.ingress.live_handoff import HandoffError, WeKnoraHandoff


class HandoffRedirectContract(unittest.TestCase):
    def test_ingest_api_key_not_forwarded_on_302(self):
        arrivals = []
        class Destination(BaseHTTPRequestHandler):
            def do_GET(self):
                arrivals.append((self.headers.get("X-API-Key"), self.headers.get("Authorization")))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"success": True, "data": {
                    "resource_ref": "resource://wrong-origin", "ownership": "external"}}).encode())
            def log_message(self, *_): pass
        destination = ThreadingHTTPServer(("127.0.0.1", 0), Destination)
        class Source(BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{destination.server_port}/credential-leak")
                self.end_headers()
            def log_message(self, *_): pass
        source = ThreadingHTTPServer(("127.0.0.1", 0), Source)
        threads = []
        try:
            for server in (source, destination):
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                threads.append(thread)
            client = WeKnoraHandoff(
                api_url=f"http://127.0.0.1:{source.server_port}/api/v1",
                api_key="synthetic-scoped-ingest-only-not-real",
                kb_id="synthetic-kb")
            with self.assertRaises(HandoffError):
                client.register_external(
                    resource_path="local://10000/aibox126/synthetic.txt",
                    size=1, sha256="a"*64, filename="synthetic.txt", mime="text/plain")
            self.assertEqual([], arrivals, "scoped ingest credential was forwarded to redirected origin")
        finally:
            for server in (source, destination):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
