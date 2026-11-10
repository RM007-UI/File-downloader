import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("downloader.py")
SPEC = importlib.util.spec_from_file_location("downloader", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DownloaderTests(unittest.TestCase):
    def test_filename_keeps_any_extension(self):
        self.assertEqual(MODULE.safe_filename("../../folder/video.mkv"), "video.mkv")
        self.assertEqual(MODULE.safe_filename(r"C:\temp\archive.tar.gz"), "archive.tar.gz")
        self.assertEqual(MODULE.safe_filename("app.apk"), "app.apk")
        self.assertEqual(MODULE.safe_filename("document.pdf"), "document.pdf")

    def test_empty_filename_fallback(self):
        self.assertEqual(MODULE.safe_filename("..."), "downloaded-file.bin")

    def test_tls_verification_is_enabled_by_default(self):
        import ssl
        context = MODULE.build_ssl_context(False)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_tls_verification_can_be_opted_out(self):
        import ssl
        context = MODULE.build_ssl_context(True)
        self.assertEqual(context.verify_mode, ssl.CERT_NONE)
        self.assertFalse(context.check_hostname)

    def test_only_http_https(self):
        for url in ("file:///etc/passwd", "ftp://example.org/file", "javascript:alert(1)"):
            with self.subTest(url=url):
                with self.assertRaises(MODULE.DownloadError):
                    MODULE.validate_public_url(url)

    def test_private_local_destinations_are_blocked(self):
        for url in (
            "http://127.0.0.1/file",
            "http://10.0.0.4/file",
            "http://192.168.1.20/file",
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost/file",
        ):
            with self.subTest(url=url):
                with self.assertRaises(MODULE.DownloadError):
                    MODULE.validate_public_url(url)


if __name__ == "__main__":
    unittest.main()
