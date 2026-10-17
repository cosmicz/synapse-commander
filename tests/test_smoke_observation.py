"""The declared goal survives HTTP filtering; private scenario details do not."""
import http.client
import json
import threading
import unittest

from synapse_commander.gateway import LoopbackGateway
from synapse_commander.protocol import ProtocolError, bounded_observation
from synapse_commander.transport import MemoryTransport


def engine_observation():
    return {
        "observation_id": "obs-0-15", "frame": 15,
        "entities": [{"id": 42, "pos": [100, 0, 100], "source": "own_unit"}],
        "scenario": {"waypoints": {"W1": [500, 0, 100], "hidden": [99, 0, 99]},
                     "gaia_far": 12345},
        "permitted_intents": [{"type": "TASK", "verbs": ["advance", "console"],
                               "diagnostic": "private"}],
        "hidden_state": {"unit": 12345},
    }


class SmokeObservationTests(unittest.TestCase):
    def test_http_client_observes_unit_and_declared_fixed_goal(self):
        transport = MemoryTransport(frame=15)
        transport.publish_observation("0", engine_observation())
        server = LoopbackGateway(transport, principal="smoke", team="0").make_server()
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", "/v1/observation")
            response = connection.getresponse()
            observed = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(observed["entities"][0]["id"], 42)
            self.assertEqual(observed["scenario"], {"waypoints": {"W1": [500, 0, 100]}})
            self.assertEqual(observed["permitted_intents"], [{"type": "TASK", "verbs": ["advance"]}])
            self.assertNotIn("hidden_state", observed)
            self.assertNotIn("12345", json.dumps(observed))
        finally:
            connection.close()
            server.shutdown()
            thread.join()
            server.server_close()

    def test_goal_and_permissions_are_never_invented(self):
        observed = bounded_observation({"observation_id": "o", "frame": 0})
        self.assertNotIn("scenario", observed)
        self.assertEqual(observed["permitted_intents"], [])
        source = engine_observation()
        source["permitted_intents"] = [{"type": "INSTALL", "verbs": ["advance"]}]
        self.assertEqual(bounded_observation(source)["permitted_intents"], [])

    def test_goal_requires_exactly_three_finite_numeric_coordinates(self):
        for goal in ([1, 2], [1, 2, 3, 4], [True, 0, 0], ["1", 0, 0],
                     [float("nan"), 0, 0], [float("inf"), 0, 0], [10**1000, 0, 0], {}):
            with self.subTest(goal=goal):
                source = engine_observation()
                source["scenario"]["waypoints"]["W1"] = goal
                with self.assertRaises(ProtocolError):
                    bounded_observation(source)

    def test_metadata_still_obeys_observation_size_cap(self):
        source = engine_observation()
        source["resources"] = {"padding": "x" * 32768}
        with self.assertRaisesRegex(ProtocolError, "exceeds public cap"):
            bounded_observation(source)
