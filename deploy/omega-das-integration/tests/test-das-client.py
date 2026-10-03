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


service_clients = types.ModuleType("hyperon_das.service_clients")
service_clients.PatternMatchingQueryProxy = object
proxy_module = types.ModuleType("hyperon_das.service_bus.proxy")
proxy_module.DistributedAlgorithmNodeManager = FakeManager
service_bus = types.ModuleType("hyperon_das.service_bus.service_bus")
service_bus.ServiceBusSingleton = object
sys.modules["hyperon_das"] = types.ModuleType("hyperon_das")
sys.modules["hyperon_das.service_clients"] = service_clients
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


if __name__ == "__main__":
    unittest.main()
