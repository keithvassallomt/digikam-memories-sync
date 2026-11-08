"""Regression tests for HTTP request construction."""
from __future__ import annotations

import unittest

from digimem.http_client import KeepAliveHttpClient


class HostHeaderTests(unittest.TestCase):
    def test_host_header_keeps_explicit_port(self) -> None:
        client = KeepAliveHttpClient("https://cloud.example.test:8443")
        self.assertEqual(client.host_header, "cloud.example.test:8443")

    def test_host_header_omits_default_port(self) -> None:
        client = KeepAliveHttpClient("https://cloud.example.test")
        self.assertEqual(client.host_header, "cloud.example.test")


if __name__ == "__main__":
    unittest.main()
