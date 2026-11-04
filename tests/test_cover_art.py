import os
import unittest
from unittest.mock import patch, MagicMock

# Set up test environment variable before importing auth
os.environ["PROXY_API_KEYS"] = "test-secret-key"

import auth
auth.PROXY_API_KEYS = {"test-secret-key"}

from server import app


class CoverArtProxyTestCase(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_missing_api_key_returns_401(self):
        """Requests without an API key must be rejected with 401 Unauthorized."""
        response = self.client.get("/cover-art?artist=Daft+Punk&album=Discovery")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json.get("error"), "Unauthorized")

    def test_invalid_api_key_returns_403(self):
        """Requests with an invalid API key must be rejected with 403 Forbidden."""
        response = self.client.get("/cover-art?artist=Daft+Punk&album=Discovery&api_key=wrong-key")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json.get("error"), "Forbidden")

    def test_missing_query_parameters_returns_400(self):
        """Requests missing artist or album must return 400 Bad Request."""
        # Missing album
        res1 = self.client.get("/cover-art?artist=Daft+Punk&api_key=test-secret-key")
        self.assertEqual(res1.status_code, 400)
        self.assertIn("required", res1.json.get("message", ""))

        # Missing artist
        res2 = self.client.get("/cover-art?album=Discovery&api_key=test-secret-key")
        self.assertEqual(res2.status_code, 400)
        self.assertIn("required", res2.json.get("message", ""))

    def test_invalid_size_parameter_returns_400(self):
        """Unsupported size values must return 400 Bad Request."""
        response = self.client.get(
            "/cover-art?artist=Daft+Punk&album=Discovery&size=gigantic&api_key=test-secret-key"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid size parameter", response.json.get("message", ""))

    @patch("routes.cover_art.requests.get")
    def test_successful_cover_art_retrieval(self, mock_get):
        """Valid artist and album should resolve MBID and stream cover image back."""
        # Mock 1: MusicBrainz release-group search
        mb_mock = MagicMock()
        mb_mock.status_code = 200
        mb_mock.json.return_value = {
            "release-groups": [{"id": "mock-mbid-1234", "title": "Discovery"}]
        }

        # Mock 2: Cover Art Archive image stream
        caa_mock = MagicMock()
        caa_mock.status_code = 200
        caa_mock.headers = {"Content-Type": "image/jpeg", "Content-Length": "12345"}
        caa_mock.iter_content.return_value = [b"fake_image_chunk_1", b"fake_image_chunk_2"]

        mock_get.side_effect = [mb_mock, caa_mock]

        response = self.client.get(
            "/cover-art?artist=Daft+Punk&album=Discovery&api_key=test-secret-key"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "image/jpeg")
        self.assertEqual(response.headers.get("X-MusicBrainz-MBID"), "mock-mbid-1234")
        self.assertEqual(response.data, b"fake_image_chunk_1fake_image_chunk_2")

    @patch("routes.cover_art.requests.get")
    def test_not_found_when_no_release_group_or_release(self, mock_get):
        """When artist/album is not found on MusicBrainz, return 404."""
        empty_mb = MagicMock()
        empty_mb.status_code = 200
        empty_mb.json.return_value = {"release-groups": [], "releases": []}
        mock_get.return_value = empty_mb

        response = self.client.get(
            "/cover-art?artist=UnknownArtist&album=UnknownAlbum&api_key=test-secret-key"
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json.get("error"), "Not Found")


if __name__ == "__main__":
    unittest.main()
