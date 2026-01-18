import os
import asyncio
import importlib.util
import unittest


def load_module():
    # import by file path because folder name contains hyphen
    here = os.path.dirname(__file__)
    path = os.path.join(here, "..", "docintel-data-processor", "main.py")
    spec = importlib.util.spec_from_file_location("docintel_dp_main", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestDocIntelDataProcessor(unittest.TestCase):
    def setUp(self):
        self.module = load_module()

    def test_get_ai_metadata_suggestions_returns_defaults(self):
        coro = self.module.get_ai_metadata_suggestions(
            "This is a short test document about accounts and procedures."
        )
        result = asyncio.run(coro)
        self.assertIsInstance(result, dict)
        for key in ("title", "department", "process_type", "status"):
            self.assertIn(key, result)
            val = result[key]
            self.assertIsInstance(val, dict)
            self.assertIn("suggested_value", val)
            # local-first stub should return None suggested_value
            self.assertIsNone(val["suggested_value"])


if __name__ == "__main__":
    unittest.main()
