#!/usr/bin/env python3
"""Docker-free unit tests for the proxy's narrow HTTP-facing contract."""

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path


class QueryFailure(RuntimeError):
    pass


fake_client = types.ModuleType("das_client")
fake_client.DasQueryRunner = object
fake_client.QueryFailure = QueryFailure
sys.modules["das_client"] = fake_client

proxy_dir = Path(__file__).resolve().parent.parent / "proxy"
spec = importlib.util.spec_from_file_location("omega_read_proxy", proxy_dir / "app.py")
module = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(module)


class FakeRunner:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure

    def query(self, tokens, max_answers, timeout):
        self.calls.append((tokens, max_answers, timeout))
        if self.failure:
            raise self.failure
        return [{"handles": ["h"], "assignments": {"X": "h"}, "strength": 1.0, "importance": 0.0}]


class QueryApplicationTest(unittest.TestCase):
    def setUp(self):
        os.environ["PROXY_MAX_ANSWERS"] = "20"
        os.environ["PROXY_MAX_QUERY_TOKENS"] = "4"
        os.environ["PROXY_QUERY_TIMEOUT_SECONDS"] = "3"

    def test_accepts_only_tokens_and_bound(self):
        runner = FakeRunner()
        status, body = module.QueryApplication(runner).handle({"tokens": ["VARIABLE", "X"], "max_answers": 2})
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        self.assertEqual(runner.calls, [(["VARIABLE", "X"], 2, 3.0)])

    def test_rejects_unknown_options_that_could_expand_semantics(self):
        runner = FakeRunner()
        status, _ = module.QueryApplication(runner).handle({"tokens": ["VARIABLE", "X"], "attention_update": True})
        self.assertEqual(status, 400)
        self.assertEqual(runner.calls, [])

    def test_rejects_invalid_limits_and_tokens(self):
        application = module.QueryApplication(FakeRunner())
        for payload in ({"tokens": []}, {"tokens": ["x"] * 5}, {"tokens": ["x"], "max_answers": 0}, {"tokens": [1]}):
            with self.subTest(payload=payload):
                self.assertEqual(application.handle(payload)[0], 400)

    def test_returns_clean_upstream_failure(self):
        application = module.QueryApplication(FakeRunner(QueryFailure("query timed out")))
        status, body = application.handle({"tokens": ["VARIABLE", "X"]})
        self.assertEqual((status, body), (502, {"error": "query timed out"}))


if __name__ == "__main__":
    unittest.main()
