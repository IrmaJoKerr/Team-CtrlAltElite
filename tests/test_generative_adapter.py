import os
import asyncio
import unittest

from adapters import generative_adapter


class TestGenerativeAdapter(unittest.TestCase):
    def test_generate_answer_returns_snippets_when_no_endpoint(self):
        # Ensure endpoint unset
        os.environ.pop('GENERATIVE_ENDPOINT', None)
        prompt = 'User question'
        snippets = [{'snippet': 'First snippet.'}, {'snippet': 'Second snippet.'}]
        result = asyncio.run(generative_adapter.generate_answer(prompt, snippets))
        self.assertIn('First snippet.', result)
        self.assertIn('Second snippet.', result)


if __name__ == '__main__':
    unittest.main()
