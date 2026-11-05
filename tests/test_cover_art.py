import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Set up test environment variable before importing auth
os.environ["PROXY_API_KEYS"] = "test-secret-key"

import auth
auth.PROXY_API_KEYS = {"test-secret-key"}

import routes.cover_art
from server import app


class CoverArtProxyTestCase(unittest.TestCase):
    def setUp(self):
        from extensions import cache
        cache.clear()
        self.test_dir = tempfile.TemporaryDirectory()
        self.original_cover_art_dir = routes.cover_art.COVER_ART_DIR
        routes.cover_art.COVER_ART_DIR = self.test_dir.name
        self.client = app.test_client()

    def tearDown(self):
        routes.cover_art.COVER_ART_DIR = self.original_cover_art_dir
        self.test_dir.cleanup()
        from extensions import cache
        cache.clear()

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

    @patch("routes.cover_art.requests.get")
    def test_serves_from_disk_on_subsequent_request(self, mock_get):
        """When an album has already been retrieved and saved, subsequent requests must serve it from disk without upstream calls."""
        mb_mock = MagicMock()
        mb_mock.status_code = 200
        mb_mock.json.return_value = {
            "release-groups": [{"id": "mock-mbid-1234", "title": "Discovery"}]
        }

        caa_mock = MagicMock()
        caa_mock.status_code = 200
        caa_mock.headers = {"Content-Type": "image/jpeg", "Content-Length": "12345"}
        caa_mock.iter_content.return_value = [b"cover_art_bytes"]

        mock_get.side_effect = [mb_mock, caa_mock]

        # First request: fetches from upstream and saves to disk
        resp1 = self.client.get(
            "/cover-art?artist=Daft+Punk&album=Discovery&api_key=test-secret-key"
        )
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.data, b"cover_art_bytes")
        self.assertEqual(mock_get.call_count, 2)

        # Verify file exists on disk
        saved_files = os.listdir(self.test_dir.name)
        self.assertEqual(len(saved_files), 1)
        self.assertEqual(saved_files[0], "Daft Punk - Discovery (original) [mock-mbid-1234].jpg")

        # Second request: must serve from saved file without making any upstream calls
        mock_get.reset_mock()
        resp2 = self.client.get(
            "/cover-art?artist=Daft+Punk&album=Discovery&api_key=test-secret-key"
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data, b"cover_art_bytes")
        self.assertEqual(resp2.headers.get("X-MusicBrainz-MBID"), "mock-mbid-1234")
        self.assertEqual(mock_get.call_count, 0)

    @patch("routes.cover_art.requests.get")
    def test_case_insensitive_saved_lookup(self, mock_get):
        """Case variations in artist and album should hit the saved cover art on disk."""
        mb_mock = MagicMock()
        mb_mock.status_code = 200
        mb_mock.json.return_value = {
            "release-groups": [{"id": "mock-mbid-1234", "title": "Discovery"}]
        }

        caa_mock = MagicMock()
        caa_mock.status_code = 200
        caa_mock.headers = {"Content-Type": "image/jpeg"}
        caa_mock.iter_content.return_value = [b"image_data"]

        mock_get.side_effect = [mb_mock, caa_mock]

        # Initial request in Title Case
        self.client.get("/cover-art?artist=Daft+Punk&album=Discovery&api_key=test-secret-key")

        # Query in lowercase
        mock_get.reset_mock()
        resp = self.client.get("/cover-art?artist=daft+punk&album=discovery&api_key=test-secret-key")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, b"image_data")
        self.assertEqual(resp.headers.get("X-MusicBrainz-MBID"), "mock-mbid-1234")
        self.assertEqual(mock_get.call_count, 0)

    @patch("routes.cover_art.requests.get")
    def test_different_sizes_cached_separately(self, mock_get):
        """Different sizes for the same album are fetched and cached as distinct files."""
        mb_mock = MagicMock()
        mb_mock.status_code = 200
        mb_mock.json.return_value = {
            "release-groups": [{"id": "mock-mbid-1234", "title": "Discovery"}]
        }

        caa_mock_500 = MagicMock()
        caa_mock_500.status_code = 200
        caa_mock_500.headers = {"Content-Type": "image/jpeg"}
        caa_mock_500.iter_content.return_value = [b"bytes_500"]

        mock_get.side_effect = [mb_mock, caa_mock_500]

        # 1. Fetch size=500
        resp1 = self.client.get("/cover-art?artist=Daft+Punk&album=Discovery&size=500&api_key=test-secret-key")
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.data, b"bytes_500")

        # 2. Subsequent size=500 served from disk
        mock_get.reset_mock()
        resp2 = self.client.get("/cover-art?artist=Daft+Punk&album=Discovery&size=500&api_key=test-secret-key")
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.data, b"bytes_500")
        self.assertEqual(mock_get.call_count, 0)

        # 3. Requesting original size triggers upstream fetch for the original
        caa_mock_orig = MagicMock()
        caa_mock_orig.status_code = 200
        caa_mock_orig.headers = {"Content-Type": "image/jpeg"}
        caa_mock_orig.iter_content.return_value = [b"bytes_orig"]
        mock_get.side_effect = [caa_mock_orig]  # MBID is already cached in memory

        resp3 = self.client.get("/cover-art?artist=Daft+Punk&album=Discovery&size=original&api_key=test-secret-key")
        self.assertEqual(resp3.status_code, 200)
        self.assertEqual(resp3.data, b"bytes_orig")
        self.assertEqual(mock_get.call_count, 1)

        # Verify both files exist on disk
        files = sorted(os.listdir(self.test_dir.name))
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0], "Daft Punk - Discovery (500) [mock-mbid-1234].jpg")
        self.assertEqual(files[1], "Daft Punk - Discovery (original) [mock-mbid-1234].jpg")

    @patch("routes.cover_art.requests.get")
    def test_upstream_failure_does_not_save_file(self, mock_get):
        """If upstream returns 404 or fails, nothing should be saved to the covers directory."""
        empty_mb = MagicMock()
        empty_mb.status_code = 200
        empty_mb.json.return_value = {"release-groups": [], "releases": []}
        mock_get.return_value = empty_mb

        resp = self.client.get("/cover-art?artist=Nonexistent&album=Nope&api_key=test-secret-key")
        self.assertEqual(resp.status_code, 404)

        saved_files = os.listdir(self.test_dir.name)
        self.assertEqual(len(saved_files), 0)


if __name__ == "__main__":
    unittest.main()
