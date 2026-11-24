"""UNIT test of the describe_screen client against a local stand-in OpenAI-compatible server. This proves the
request/response handling only; it is NOT evidence that any real vision model describes screenshots well."""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from aw import vision  # noqa: E402

SEEN = {}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers["Content-Length"])
        SEEN["body"] = json.loads(self.rfile.read(n))
        SEEN["auth"] = self.headers.get("Authorization")
        SEEN["path"] = self.path
        out = json.dumps({"choices": [{"message": {"content": " A calculator window. "}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


class Vision(unittest.TestCase):
    def test_request_shape_and_key_from_named_env_var(self):
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ.update({"AW_VISION_URL": f"http://127.0.0.1:{srv.server_port}/v1", "AW_VISION_MODEL": "m",
                           "AW_VISION_KEY_ENV": "MY_KEY", "MY_KEY": "k123"})
        try:
            text = vision.describe("AAAA", 10, 5, "what is open?")
        finally:
            srv.shutdown()
            for k in ("AW_VISION_URL", "AW_VISION_MODEL", "AW_VISION_KEY_ENV", "MY_KEY"):
                os.environ.pop(k, None)
        self.assertEqual(text, "A calculator window.")
        self.assertEqual(SEEN["path"], "/v1/chat/completions")
        self.assertEqual(SEEN["auth"], "Bearer k123")
        part = SEEN["body"]["messages"][0]["content"][1]
        self.assertTrue(part["image_url"]["url"].startswith("data:image/png;base64,AAAA"))
        self.assertIn("what is open?", SEEN["body"]["messages"][0]["content"][0]["text"])

    def test_unconfigured_is_an_explicit_error(self):
        for k in ("AW_VISION_URL", "AW_VISION_MODEL"):
            os.environ.pop(k, None)
        with self.assertRaises(vision.VisionUnavailable):
            vision.describe("AAAA", 1, 1)

    def test_unreachable_backend_is_reported_not_invented(self):
        os.environ.update({"AW_VISION_URL": "http://127.0.0.1:9/v1", "AW_VISION_MODEL": "m"})
        try:
            with self.assertRaises(vision.VisionUnavailable):
                vision.describe("AAAA", 1, 1, timeout=2)
        finally:
            del os.environ["AW_VISION_URL"], os.environ["AW_VISION_MODEL"]


if __name__ == "__main__":
    unittest.main()
