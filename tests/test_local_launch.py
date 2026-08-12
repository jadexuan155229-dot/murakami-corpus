"""本地启动时浏览器自动打开的行为。"""

import unittest
from unittest.mock import patch

import app as webapp


class LocalLaunchTests(unittest.TestCase):
    def test_opens_browser_after_local_server_accepts_connections(self):
        with patch.dict(webapp.os.environ, {"WSL_DISTRO_NAME": ""}), patch.object(
            webapp.socket, "create_connection"
        ) as connect, patch.object(webapp.webbrowser, "open_new_tab") as open_browser:
            webapp.open_local_browser_when_ready()

        connect.assert_called_once_with(("127.0.0.1", 5731), timeout=0.25)
        open_browser.assert_called_once_with(webapp.LOCAL_URL)

    def test_wsl_uses_the_windows_default_browser(self):
        with patch.dict(webapp.os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}), patch.object(
            webapp.subprocess, "Popen"
        ) as open_browser:
            webapp.open_local_browser()

        open_browser.assert_called_once_with(
            ["cmd.exe", "/c", "start", "", webapp.LOCAL_URL],
            stdout=webapp.subprocess.DEVNULL,
            stderr=webapp.subprocess.DEVNULL,
        )


if __name__ == "__main__":
    unittest.main()
