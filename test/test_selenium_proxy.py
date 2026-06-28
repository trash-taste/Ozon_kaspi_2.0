import os
import unittest

from selenium.webdriver.chrome.options import Options

from src.utils.selenium_manager import SeleniumManager, normalize_proxy_server


class SeleniumProxyTests(unittest.TestCase):
    def test_normalizes_host_port_proxy(self):
        self.assertEqual(
            normalize_proxy_server("10.20.30.40:8000"),
            "http://10.20.30.40:8000",
        )

    def test_keeps_supported_proxy_scheme(self):
        self.assertEqual(
            normalize_proxy_server("socks5://10.20.30.40:1080"),
            "socks5://10.20.30.40:1080",
        )

    def test_rejects_proxy_with_credentials(self):
        with self.assertRaises(ValueError):
            normalize_proxy_server("http://user:pass@example.com:8000")

    def test_adds_proxy_arguments_from_environment(self):
        options = Options()
        manager = SeleniumManager()

        self.addCleanup(os.environ.pop, "OZON_PROXY_URL", None)
        self.addCleanup(os.environ.pop, "OZON_PROXY_BYPASS", None)
        os.environ["OZON_PROXY_URL"] = "10.20.30.40:8000"
        os.environ["OZON_PROXY_BYPASS"] = "localhost,127.0.0.1"

        manager._apply_proxy_options(options)

        self.assertIn("--proxy-server=http://10.20.30.40:8000", options.arguments)
        self.assertIn("--proxy-bypass-list=localhost,127.0.0.1", options.arguments)


if __name__ == "__main__":
    unittest.main()
