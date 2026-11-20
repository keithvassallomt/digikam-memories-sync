"""The local server's handling of a page that goes away mid-request."""
import contextlib
import io
import unittest

from digimem.local_server import DigiMemHTTPServer


class ClosedConnectionTests(unittest.TestCase):
    def setUp(self):
        self.server = DigiMemHTTPServer(("127.0.0.1", 0), app=None)

    def tearDown(self):
        self.server.server_close()

    def error_output(self, error: Exception) -> str:
        printed = io.StringIO()
        with contextlib.redirect_stderr(printed):
            try:
                raise error
            except Exception:
                self.server.handle_error(None, ("127.0.0.1", 43310))
        return printed.getvalue()

    def test_a_page_that_let_go_prints_nothing(self):
        self.assertEqual(self.error_output(BrokenPipeError(32, "Broken pipe")), "")
        self.assertEqual(self.error_output(ConnectionResetError(104, "reset")), "")

    def test_anything_else_still_prints(self):
        self.assertIn("ValueError: real trouble", self.error_output(ValueError("real trouble")))


if __name__ == "__main__":
    unittest.main()
