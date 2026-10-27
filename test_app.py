import unittest
from app import normalize_instagram_url, select_video_info


class TestVideoLinks(unittest.TestCase):
    def test_reel(self):
        self.assertEqual(normalize_instagram_url('https://www.instagram.com/reel/Cx1234567/?igsh=abc'),
                         'https://www.instagram.com/reel/Cx1234567/')

    def test_post(self):
        self.assertEqual(normalize_instagram_url('https://instagram.com/p/ABCdef12345/'),
                         'https://www.instagram.com/p/ABCdef12345/')

    def test_non_instagram(self):
        for url in ('https://instagram.com.evil.test/reel/ABCdef12345/',
                    'http://www.instagram.com/reel/ABCdef12345/',
                    'https://www.instagram.com@evil.test/reel/ABCdef12345/',
                    'file:///etc/passwd',
                    'https://127.0.0.1/reel/ABCdef12345/',
                    'https://www.instagram.com/other/user/'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_instagram_url(url)

    def test_photo_rejected(self):
        with self.assertRaises(ValueError):
            select_video_info({'ext': 'jpg', 'formats': []})

    def test_video_accepted(self):
        self.assertTrue(select_video_info({'ext': 'mp4', 'id': 'abc'}))


if __name__ == '__main__':
    unittest.main()
