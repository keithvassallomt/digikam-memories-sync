import json
import unittest
from urllib.parse import parse_qs, urlparse

from digikam_nextcloud.models import FaceRegion, NextcloudFile, Rect
from digikam_nextcloud.nextcloud_http import (
    FACE_SYNC_APP_INSTALL_URL,
    NextcloudConnectionError,
    NextcloudHTTP,
    RecognizeNotInstalledError,
)


class FaceImportHTTPTests(unittest.TestCase):
    def make_backend(self):
        backend = NextcloudHTTP.__new__(NextcloudHTTP)
        backend.timeout = 60.0
        backend.supports_insert = True
        backend.supports_export = True
        backend.supports_assign = False
        return backend

    def test_capability_probe_enables_only_matching_api(self):
        backend = self.make_backend()
        backend._request = lambda *args, **kwargs: (
            200,
            {},
            json.dumps(
                {
                    "apiVersion": 4,
                    "createFaceDetection": True,
                    "listFaceDetections": True,
                    "assignFaceDetection": True,
                    "confirmedFaceImport": True,
                }
            ).encode(),
        )

        self.assertEqual(
            backend._probe_face_sync_app(),
            {"create": True, "list": True, "assign": True, "confirmed": True},
        )

        backend._request = lambda *args, **kwargs: (404, {}, b"")
        self.assertEqual(
            backend._probe_face_sync_app(),
            {"create": False, "list": False, "assign": False, "confirmed": False},
        )

    def test_named_face_export_is_paginated_and_parsed(self):
        backend = self.make_backend()
        backend.face_list_url = lambda: "index.php/apps/digikam_face_sync/api/v1/faces"
        pages = {
            0: {
                "detections": [
                    {
                        "id": 7,
                        "fileId": 11,
                        "path": "Photos/2026/one.jpg",
                        "name": "one.jpg",
                        "person": "Gail Vassallo",
                        "x": 0.1,
                        "y": 0.2,
                        "width": 0.3,
                        "height": 0.4,
                        "clusterId": 4,
                        "threshold": 0.8,
                    }
                ],
                "nextAfter": 7,
            },
            7: {
                "detections": [
                    {
                        "id": 9,
                        "fileId": 12,
                        "path": "Photos/2026/two.jpg",
                        "name": "two.jpg",
                        "person": "Gail Vassallo",
                        "x": 0.2,
                        "y": 0.2,
                        "width": 0.2,
                        "height": 0.2,
                        "clusterId": 4,
                    }
                ],
                "nextAfter": None,
            },
        }
        requested = []

        def request(method, path, **kwargs):
            query = parse_qs(urlparse(path).query)
            requested.append(query)
            after = int(query["after"][0])
            return 200, {}, json.dumps(pages[after]).encode()

        backend._request = request
        progress = []
        faces = backend.list_named_faces(
            "Gail Vassallo", page_size=1, progress_callback=progress.append
        )

        self.assertEqual([face.face.nc_detection_id for face in faces], [7, 9])
        self.assertEqual(faces[0].file.webdav_path, "Photos/2026/one.jpg")
        self.assertEqual(faces[0].face.person, "Gail Vassallo")
        self.assertEqual(progress, [1, 2])
        self.assertEqual(requested[0]["person"], ["Gail Vassallo"])

    def test_recognize_installation_probe_distinguishes_missing_and_bad_login(self):
        backend = self.make_backend()
        backend.recognize_dav_base = lambda: "remote.php/dav/recognize/keith"

        backend._request = lambda *args, **kwargs: (207, {}, b"")
        self.assertTrue(backend._probe_recognize_installation())

        backend._request = lambda *args, **kwargs: (404, {}, b"")
        self.assertFalse(backend._probe_recognize_installation())

        backend._request = lambda *args, **kwargs: (401, {}, b"")
        with self.assertRaisesRegex(NextcloudConnectionError, "username or app password"):
            backend._probe_recognize_installation()

    def test_missing_recognize_is_an_actionable_error(self):
        backend = self.make_backend()
        backend._recognize_ready = None
        backend._recognize_error = None
        backend.recognize_api_key = ""
        backend.recognize_dav_base = lambda: "remote.php/dav/recognize/keith"
        backend._request = lambda *args, **kwargs: (404, {}, b"")

        with self.assertRaisesRegex(RecognizeNotInstalledError, "requires Recognize"):
            backend.ensure_recognize_access()

    def test_connection_requirements_include_temporary_install_page(self):
        backend = self.make_backend()
        backend.recognize_installed = True
        backend.supports_insert = False
        backend.supports_export = False

        requirements = backend.connection_requirements()

        self.assertFalse(requirements.ready)
        self.assertTrue(requirements.recognize_installed)
        self.assertFalse(requirements.face_sync_installed)
        self.assertEqual(requirements.face_sync_install_url, FACE_SYNC_APP_INSTALL_URL)

    def test_insert_sends_person_and_normalized_rectangle(self):
        backend = self.make_backend()
        captured = {}

        def request(method, path, **kwargs):
            captured.update(method=method, path=path, **kwargs)
            return 201, {}, json.dumps({"detection": {"id": 987}}).encode()

        backend._request = request
        detection_id = backend.insert_detection(
            file_id=123,
            rect=Rect(0.1, 0.2, 0.3, 0.4),
            cluster_id=456,
            person="Gail Vassallo",
        )

        self.assertEqual(detection_id, 987)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(
            captured["path"],
            "index.php/apps/digikam_face_sync/api/v1/face-import",
        )
        self.assertEqual(
            json.loads(captured["body"]),
            {
                "fileId": 123,
                "person": "Gail Vassallo",
                "x": 0.1,
                "y": 0.2,
                "width": 0.3,
                "height": 0.4,
            },
        )
        self.assertEqual(captured["timeout"], 420.0)

    def test_insert_surfaces_server_error(self):
        backend = self.make_backend()
        backend._request = lambda *args, **kwargs: (
            409,
            {},
            json.dumps({"error": "overlap"}).encode(),
        )

        with self.assertRaisesRegex(RuntimeError, r"HTTP 409.*overlap"):
            backend.insert_detection(
                file_id=123,
                rect=Rect(0.1, 0.2, 0.3, 0.4),
                cluster_id=456,
                person="Gail Vassallo",
            )

    def test_confirmed_insert_marks_a_user_reviewed_face(self):
        backend = self.make_backend()
        backend.supports_confirmed_insert = True
        captured = {}

        def request(method, path, **kwargs):
            captured.update(method=method, path=path, **kwargs)
            return 201, {}, json.dumps({"detection": {"id": 987}}).encode()

        backend._request = request
        backend.insert_detection(
            file_id=123,
            rect=Rect(0.1, 0.2, 0.3, 0.4),
            cluster_id=456,
            person="Gail Vassallo",
            confirmed=True,
        )

        self.assertTrue(json.loads(captured["body"])["confirmed"])

    def test_companion_assignment_handles_unclustered_detection(self):
        backend = self.make_backend()
        backend.supports_assign = True
        captured = {}

        def request(method, path, **kwargs):
            captured.update(method=method, path=path, **kwargs)
            return 200, {}, json.dumps({"changed": True}).encode()

        backend._request = request
        face = FaceRegion(
            person="", rect=Rect(0.1, 0.2, 0.3, 0.4), source="nextcloud",
            nc_file_id=123, nc_detection_id=987, nc_cluster_id=None,
            dav_parent="", file_name="photo.jpg",
        )
        nc_file = NextcloudFile(
            file_id=123, path="Photos/photo.jpg", name="photo.jpg", size=0,
        )

        backend.assign_person(face, "Gail Vassallo", nc_file, 0)

        self.assertEqual(captured["method"], "POST")
        self.assertEqual(
            captured["path"],
            "index.php/apps/digikam_face_sync/api/v1/face-assign",
        )
        self.assertEqual(
            json.loads(captured["body"]),
            {"fileId": 123, "detectionId": 987, "person": "Gail Vassallo"},
        )
        self.assertEqual(face.person, "Gail Vassallo")
        self.assertEqual(face.dav_parent, "Gail Vassallo")

    def test_numeric_cluster_fallback_is_not_treated_as_a_person(self):
        backend = self.make_backend()
        nc_file = NextcloudFile(
            file_id=226205,
            path="files/Photos/test.jpg",
            name="test.jpg",
            size=0,
        )
        payload = json.dumps(
            [
                {
                    "id": 11147,
                    "clusterId": 241,
                    "title": "241",
                    "x": 0.1,
                    "y": 0.2,
                    "width": 0.3,
                    "height": 0.4,
                },
                {
                    "id": 11148,
                    "clusterId": 242,
                    "title": "Gail Vassallo",
                    "x": 0.2,
                    "y": 0.3,
                    "width": 0.2,
                    "height": 0.2,
                },
            ]
        )
        raw = f'''<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:nc="http://nextcloud.org/ns">
  <d:response><d:propstat><d:prop>
    <nc:face-detections>{payload}</nc:face-detections>
  </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
</d:multistatus>'''.encode()

        faces = backend._parse_face_detections(raw, nc_file)

        self.assertEqual(faces[0].person, "")
        self.assertEqual(faces[0].dav_parent, "241")
        self.assertEqual(faces[0].nc_cluster_id, 241)
        self.assertEqual(faces[1].person, "Gail Vassallo")
        self.assertEqual(faces[1].dav_parent, "Gail Vassallo")


if __name__ == "__main__":
    unittest.main()
