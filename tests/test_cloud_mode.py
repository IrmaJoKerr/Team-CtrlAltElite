import os
import sys
import unittest

from utils.cloud_mode import detect_cloud_mode


class TestDetectCloudMode(unittest.TestCase):
    def test_cli_flag_overrides(self):
        argv = ["--cloud-mode"]
        env = {}
        gl = {"CLOUD_MODE": False}
        val, src = detect_cloud_mode(argv, env, gl)
        self.assertTrue(val)
        self.assertEqual(src, "cli")

    def test_cli_token_overrides(self):
        argv = ["run", "cloudmode"]
        val, src = detect_cloud_mode(argv, {}, {})
        self.assertTrue(val)
        self.assertEqual(src, "cli")

    def test_env_true(self):
        argv = []
        env = {"CLOUD_MODE": "true"}
        val, src = detect_cloud_mode(argv, env, {})
        self.assertTrue(val)
        self.assertEqual(src, "env")

    def test_global_true(self):
        argv = []
        env = {}
        gl = {"CLOUD_MODE": True}
        val, src = detect_cloud_mode(argv, env, gl)
        self.assertTrue(val)
        self.assertEqual(src, "global")

    def test_default_false(self):
        argv = []
        env = {}
        gl = {}
        val, src = detect_cloud_mode(argv, env, gl)
        self.assertFalse(val)
        self.assertEqual(src, "default")


if __name__ == "__main__":
    unittest.main()
