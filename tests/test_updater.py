import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import updater


class UpdaterTest(unittest.TestCase):
    def test_windows_installer_uses_shell_to_request_elevation(self):
        with tempfile.TemporaryDirectory() as directory:
            installer = Path(directory) / 'setup.exe'
            installer.touch()
            with patch.object(updater.os, 'name', 'nt'), patch.object(
                updater.os, 'startfile', create=True
            ) as startfile:
                updater.launch_installer(installer)

        startfile.assert_called_once_with(str(installer))


if __name__ == '__main__':
    unittest.main()
