import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from app import app


class TestApi(unittest.TestCase):
    def test_health_and_static_page(self):
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/health').json(), {'status': 'ok'})
            page = client.get('/')
            self.assertEqual(page.status_code, 200)
            self.assertIn('Save Instagram videos', page.text)
            self.assertIn('style-src', page.headers['content-security-policy'])

    def test_invalid_url_never_hits_extractor(self):
        with TestClient(app) as client, patch('app._inspect') as do_inspect:
            response = client.post('/api/inspect', json={'url': 'https://badhost.example/reel/abcdefghijk/'})
            self.assertEqual(response.status_code, 400)
            do_inspect.assert_not_called()

    def test_inspect_prepare_and_single_use_file(self):
        url = 'https://www.instagram.com/reel/ABCdef12345/'
        def fake_download(_url: str, folder: Path) -> Path:
            f = folder / 'rv-test.mp4'
            f.write_bytes(b'mock-mp4-media-test')
            return f
        preview = {'title': 'Test video', 'thumbnail': '', 'duration': 11, 'uploader': 'test'}
        with TestClient(app) as client, patch('app._inspect', return_value=preview), patch('app._download', side_effect=fake_download):
            inspect = client.post('/api/inspect', json={'url': url})
            self.assertEqual(inspect.status_code, 200)
            payload = inspect.json()
            self.assertEqual(payload['title'], 'Test video')
            self.assertEqual(payload['type'], 'Reel')
            self.assertTrue(payload.get('token'))
            prepare = client.post('/api/prepare', json={'token': payload['token']})
            self.assertEqual(prepare.status_code, 200)
            path = prepare.json()['download_url']
            file_response = client.get(path)
            self.assertEqual(file_response.status_code, 200)
            self.assertEqual(file_response.content, b'mock-mp4-media-test')
            self.assertEqual(client.get(path).status_code, 404)


if __name__ == '__main__':
    unittest.main()
