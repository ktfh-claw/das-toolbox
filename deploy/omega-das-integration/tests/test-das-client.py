#!/usr/bin/env python3
"""Docker-free regression tests for DAS callback peer rewriting."""

import importlib.util
import sys
import types
import unittest
from pathlib import Path


class FakeManager:
    def node_joined_network(self, node_id):
        self.peer_id = node_id


class FakeAssignment:
    def __init__(self):
        self._mapping = {}

    def assign(self, label, value):
        self._mapping[label] = value


class FakeQueryAnswer:
    def __init__(self):
        self.handles = []
        self.assignment = FakeAssignment()
        self.metta_expression = {}

    def untokenize(self, token_str):
        tokens = token_str.split()
        self.strength = float(tokens[0])
        self.importance = float(tokens[1])
        flat_handle_count = int(tokens[2])
        assignment_offset = 3 + flat_handle_count
        int(tokens[assignment_offset])


service_clients = types.ModuleType("hyperon_das.service_clients")
service_clients.PatternMatchingQueryProxy = object
proxy_module = types.ModuleType("hyperon_das.service_bus.proxy")
proxy_module.DistributedAlgorithmNodeManager = FakeManager
service_bus = types.ModuleType("hyperon_das.service_bus.service_bus")
service_bus.ServiceBusSingleton = object
sys.modules["hyperon_das"] = types.ModuleType("hyperon_das")
sys.modules["hyperon_das.service_clients"] = service_clients
query_answer = types.ModuleType("hyperon_das.query_answer")
query_answer.QueryAnswer = FakeQueryAnswer
sys.modules["hyperon_das.query_answer"] = query_answer
sys.modules["hyperon_das.service_bus"] = types.ModuleType("hyperon_das.service_bus")
sys.modules["hyperon_das.service_bus.proxy"] = proxy_module
sys.modules["hyperon_das.service_bus.service_bus"] = service_bus

proxy_dir = Path(__file__).resolve().parent.parent / "proxy"
spec = importlib.util.spec_from_file_location("omega_das_client", proxy_dir / "das_client.py")
module = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(module)


class CallbackPeerRewriteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        module._install_callback_peer_rewrite("query-engine")

    def test_rewrites_ipv4_wildcard_and_preserves_dynamic_port(self):
        manager = FakeManager()
        manager.node_joined_network("0.0.0.0:42017")
        self.assertEqual(manager.peer_id, "query-engine:42017")

    def test_leaves_non_wildcard_peer_unchanged(self):
        manager = FakeManager()
        manager.node_joined_network("another-private-peer:42017")
        self.assertEqual(manager.peer_id, "another-private-peer:42017")

    def test_rejects_endpoint_or_uri_as_rewrite_host(self):
        for invalid in ("", "query-engine:40002", "dns:///query-engine"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    module._install_callback_peer_rewrite(invalid)


class NestedHandleDecoderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        module._install_nested_handle_decoder()

    def test_decodes_cpp_nested_handle_vectors_and_assignments(self):
        hashes = [f"{number:032x}" for number in range(1, 6)]
        wire_answer = (
            f"1.000000 0.000000 3 1 {hashes[0]} 2 {hashes[1]} {hashes[2]} "
            f"1 {hashes[3]} 2 L {hashes[1]} R {hashes[4]} 0"
        )

        answer = FakeQueryAnswer()
        answer.untokenize(wire_answer)

        self.assertEqual(answer.handles, hashes[:4])
        self.assertEqual(answer.assignment._mapping, {"L": hashes[1], "R": hashes[4]})
        self.assertEqual((answer.strength, answer.importance), (1.0, 0.0))

    def test_preserves_dependency_decoder_for_flat_answers(self):
        answer = FakeQueryAnswer()
        answer.untokenize("1.0 0.0 0 0")
        self.assertEqual(answer.handles, [])

    def test_rejects_unbounded_or_unexpected_nested_payloads(self):
        for wire_answer in (
            "1.0 0.0 65",
            "1.0 0.0 0 65",
            "1.0 0.0 0 0 1 expression",
        ):
            with self.subTest(wire_answer=wire_answer):
                with self.assertRaises(ValueError):
                    module._untokenize_nested_handle_answer(FakeQueryAnswer(), wire_answer)


if __name__ == "__main__":
    unittest.main()
