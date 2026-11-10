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

    def test_plan_ranges_cover_file_without_gaps(self):
        total = 101 * MODULE.MIN_PARALLEL_SIZE
        ranges = MODULE.plan_ranges(total, 8)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], total - 1)
        for previous, current in zip(ranges, ranges[1:]):
            self.assertEqual(previous[1] + 1, current[0])
        self.assertEqual(sum(end - start + 1 for start, end in ranges), total)

    def test_plan_ranges_respects_32_connection_cap(self):
        total = 512 * 1024 * 1024
        ranges = MODULE.plan_ranges(total, 99)
        self.assertEqual(len(ranges), MODULE.MAX_ALLOWED_CONNECTIONS)
        self.assertEqual(len(ranges), 32)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], total - 1)

    def test_plan_ranges_adapts_to_small_files(self):
        total = 3 * 1024 * 1024
        ranges = MODULE.plan_ranges(total, 32)
        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges, [(0, total - 1)])

    def test_plan_ranges_uses_more_connections_for_medium_files(self):
        total = 64 * 1024 * 1024
        ranges = MODULE.plan_ranges(total, 32)
        self.assertEqual(len(ranges), 16)

    def test_parse_content_range(self):
        self.assertEqual(MODULE.parse_content_range("bytes 0-0/12345"), (0, 0, 12345))
        self.assertIsNone(MODULE.parse_content_range("bytes */12345"))
        self.assertIsNone(MODULE.parse_content_range("invalid"))

    def test_progress_for_known_file_size(self):
        message = MODULE.progress_message(50 * 1024 * 1024, 100 * 1024 * 1024, 10)
        self.assertIn("50.0%", message)
        self.assertIn("Downloaded:", message)
        self.assertIn("Remaining:", message)
        self.assertIn("Speed:", message)
        self.assertIn("ETA:", message)

    def test_progress_when_total_size_is_unknown(self):
        message = MODULE.progress_message(5 * 1024 * 1024, None, 5)
        self.assertIn("Total: unknown", message)
        self.assertIn("Remaining: unknown", message)
        self.assertIn("Progress: n/a", message)

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
