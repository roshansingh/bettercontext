from __future__ import annotations

import unittest
import unittest.mock
from unittest.mock import patch


class EngineVersionTest(unittest.TestCase):
    def setUp(self) -> None:
        # Clear cache before each test so monkeypatches take effect.
        import source.kg.product.engine_version as _mod
        _mod.engine_version.cache_clear()

    def tearDown(self) -> None:
        import source.kg.product.engine_version as _mod
        _mod.engine_version.cache_clear()

    def test_returns_nonempty_string(self) -> None:
        from source.kg.product.engine_version import engine_version
        result = engine_version()
        self.assertIsInstance(result, str)
        self.assertTrue(result, "engine_version() returned empty string")

    def test_cached_same_value_across_calls(self) -> None:
        from source.kg.product.engine_version import engine_version
        first = engine_version()
        second = engine_version()
        self.assertEqual(first, second)

    def test_fallback_to_pkg_when_git_absent(self) -> None:
        """When subprocess raises (git not on PATH), must not raise, must return string."""
        import source.kg.product.engine_version as _mod
        _mod.engine_version.cache_clear()
        with patch("subprocess.run", side_effect=FileNotFoundError("no git")):
            result = _mod.engine_version()
        self.assertIsInstance(result, str)
        self.assertTrue(result)

    def test_fallback_to_unknown_when_both_fail(self) -> None:
        import source.kg.product.engine_version as _mod
        _mod.engine_version.cache_clear()
        with patch("subprocess.run", side_effect=OSError("no git")):
            with patch("importlib.metadata.version", side_effect=Exception("not installed")):
                result = _mod.engine_version()
        self.assertEqual(result, "unknown")

    def test_never_raises_even_with_git_error_exit(self) -> None:
        import subprocess
        import source.kg.product.engine_version as _mod
        _mod.engine_version.cache_clear()
        fake = unittest.mock.MagicMock()
        fake.returncode = 128
        fake.stdout = ""
        with patch("subprocess.run", return_value=fake):
            with patch("importlib.metadata.version", side_effect=Exception("not installed")):
                result = _mod.engine_version()
        self.assertEqual(result, "unknown")


if __name__ == "__main__":
    unittest.main()
