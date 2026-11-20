"""A slow Memories face list is read in smaller pieces instead of looping.

Issue #3: one page of 1,000 faces outlasted the 60 second wait on a slow
server. DigiMem sent the same request three times, gave up, and the next run
read the list from the start and stopped at the same page, for ever.
"""
import json
import unittest
from urllib.parse import parse_qs, urlparse

from digimem.http_client import HttpTimeout, KeepAliveHttpClient
from digimem.nextcloud_http import NextcloudHTTP


def detection(detection_id: int) -> dict:
    return {
        "id": detection_id,
        "fileId": 1000 + detection_id,
        "path": f"Photos/{detection_id}.jpg",
        "name": f"{detection_id}.jpg",
        "person": "Gail Vassallo",
        "x": 0.1,
        "y": 0.1,
        "width": 0.2,
        "height": 0.2,
        "clusterId": 7,
    }


class SlowServer:
    """Answers the face list, timing out on pages larger than ``slow_above``
    that start inside ``slow``."""

    def __init__(self, total: int, slow: range, slow_above: int):
        self.total = total
        self.slow = slow
        self.slow_above = slow_above
        self.asked: list[tuple[int, int]] = []

    def __call__(self, method, path, **kwargs):
        query = parse_qs(urlparse(path).query)
        after = int(query["after"][0])
        limit = int(query["limit"][0])
        self.asked.append((after, limit))
        assert kwargs.get("retry_timeouts") is False, "a slow page must not be repeated as it was"
        if after in self.slow and limit > self.slow_above:
            raise HttpTimeout("HTTP GET faces timed out after 60 seconds")
        ids = list(range(after + 1, min(self.total, after + limit) + 1))
        more = ids and ids[-1] < self.total
        body = {"detections": [detection(i) for i in ids], "nextAfter": ids[-1] if more else None}
        return 200, {}, json.dumps(body).encode()


def make_backend(server) -> NextcloudHTTP:
    backend = NextcloudHTTP.__new__(NextcloudHTTP)
    backend.timeout = 60.0
    backend.supports_export = True
    backend._request = server
    return backend


class SlowFaceListTests(unittest.TestCase):
    def test_a_page_that_times_out_is_asked_for_smaller_from_the_same_place(self):
        server = SlowServer(total=2600, slow=range(1000, 10**9), slow_above=250)
        faces = make_backend(server).list_named_faces()
        self.assertEqual(len(faces), 2600)
        self.assertEqual(
            [face.face.nc_detection_id for face in faces], list(range(1, 2601)),
            "every face once, in order",
        )
        self.assertEqual(server.asked[:3], [(0, 1000), (1000, 1000), (1000, 250)])

    def test_quick_pages_let_the_size_grow_back(self):
        server = SlowServer(total=4000, slow=range(1000, 1250), slow_above=250)
        faces = make_backend(server).list_named_faces()
        self.assertEqual(len(faces), 4000)
        self.assertEqual(server.asked[-1][1], 1000, "back to full pages once past it")

    def test_a_server_too_slow_for_the_smallest_page_still_fails(self):
        server = SlowServer(total=100, slow=range(0, 10**9), slow_above=0)
        with self.assertRaises(HttpTimeout):
            make_backend(server).list_named_faces()
        self.assertEqual([limit for _after, limit in server.asked], [1000, 250, 50, 10])


class FakeConnection:
    sock = None
    timeout = None

    def __init__(self, log):
        self.log = log

    def request(self, *args, **kwargs):
        self.log.append("request")

    def getresponse(self):
        raise TimeoutError("The read operation timed out")

    def close(self):
        pass


class TimeoutRetryTests(unittest.TestCase):
    def make_client(self, log):
        client = KeepAliveHttpClient("https://cloud.test", timeout=60.0)
        client._conn = lambda: FakeConnection(log)
        return client

    def test_a_timeout_is_not_repeated_when_the_caller_asks(self):
        log = []
        with self.assertRaises(HttpTimeout):
            self.make_client(log).request("GET", "/faces", retry_timeouts=False)
        self.assertEqual(len(log), 1)

    def test_other_callers_still_retry_and_can_tell_it_was_a_timeout(self):
        log = []
        with self.assertRaises(HttpTimeout):
            self.make_client(log).request("GET", "/faces")
        self.assertEqual(len(log), 3)


if __name__ == "__main__":
    unittest.main()
