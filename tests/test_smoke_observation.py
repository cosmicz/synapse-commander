"""The declared goal survives HTTP filtering; private scenario details do not."""
import http.client
import json
import threading
import unittest

from synapse_commander.gateway import LoopbackGateway
from synapse_commander.protocol import ProtocolError, bounded_observation
from synapse_commander.scenarios import load_scenario
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


def provisioning_observation():
    value = engine_observation()
    value.update({
        "entities": [{"id": 42, "pos": [100, 0, 100], "source": "own_unit", "controller": "reasoner_large_v1"}],
        "scenario": {"id": "rescue_smoke", "version": "1-provisional", "waypoints": {"W1": [500, 0, 100]}},
        "commander": {"unit_id": 42, "chip": "reasoner_large_v1"},
        "budgets": {"bodies_remaining": 0, "chips": {"reasoner_large_v1": 0, "reasoner_small_v1": 1, "escort_fixed_v1": 1}},
        "factories": [{"name": "F0", "unit_id": 7, "jobs": [{
            "job_id": "job-0", "intent_id": "intent-0", "chassis": "scout_v1", "controller": "reasoner_small_v1",
            "started_frame": 15, "done_frame": 135,
        }]}],
        "permitted_intents": [
            {"type": "TASK", "verbs": ["advance"]},
            {"type": "PRODUCE", "factories": ["F0"], "chassis": ["scout_v1", "carrier_v1", "relay_support_v1"],
             "controllers": ["reasoner_large_v1", "reasoner_small_v1", "escort_fixed_v1"]},
        ],
    })
    return value


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

    def test_provisioning_data_is_typed_bounded_and_keeps_depleted_choices_visible(self):
        observed = bounded_observation(provisioning_observation(), scenario_table=load_scenario())
        self.assertEqual(observed["scenario"]["id"], "rescue_smoke")
        self.assertEqual(observed["scenario"]["version"], "1-provisional")
        self.assertEqual(observed["commander"], {"unit_id": 42, "chip": "reasoner_large_v1"})
        self.assertEqual(observed["budgets"]["bodies_remaining"], 0)
        self.assertEqual(observed["budgets"]["chips"]["reasoner_large_v1"], 0)
        self.assertEqual(observed["entities"][0]["controller"], "reasoner_large_v1")
        self.assertEqual(observed["factories"][0]["jobs"][0]["controller"], "reasoner_small_v1")
        self.assertEqual(observed["factories"][0]["jobs"][0]["done_frame"], 135)
        produce = next(item for item in observed["permitted_intents"] if item["type"] == "PRODUCE")
        self.assertEqual(produce["factories"], ["F0"])
        self.assertIn("reasoner_large_v1", produce["controllers"])

    def test_provisioning_requires_the_declared_scenario_and_known_vocabularies(self):
        source = provisioning_observation()
        source["scenario"].pop("version")
        with self.assertRaisesRegex(ProtocolError, "declared scenario"):
            bounded_observation(source, scenario_table=load_scenario())
        source = provisioning_observation()
        source["factories"][0]["name"] = "hidden_factory"
        with self.assertRaisesRegex(ProtocolError, "unknown factory"):
            bounded_observation(source, scenario_table=load_scenario())

    def test_factory_job_list_is_clipped_and_counted(self):
        source = provisioning_observation()
        source["factories"][0]["jobs"] = [
            {"job_id": f"job-{number}", "intent_id": f"intent-{number}", "chassis": "scout_v1", "controller": "escort_fixed_v1", "started_frame": number, "done_frame": number + 1}
            for number in range(9)
        ]
        observed = bounded_observation(source, scenario_table=load_scenario())
        self.assertEqual(len(observed["factories"][0]["jobs"]), 8)
        self.assertEqual(observed["omitted"]["factory_jobs"], 1)
        self.assertTrue(observed["truncated"])

    def test_factory_job_allows_an_unfinished_null_done_frame(self):
        source = provisioning_observation()
        source["factories"][0]["jobs"][0]["done_frame"] = None
        self.assertIsNone(bounded_observation(source, scenario_table=load_scenario())["factories"][0]["jobs"][0]["done_frame"])

    def test_foreign_entity_controller_is_not_exposed(self):
        source = provisioning_observation()
        source["entities"].append({"id": 99, "pos": [1, 0, 1], "source": "los", "controller": "reasoner_large_v1"})
        foreign = next(item for item in bounded_observation(source, scenario_table=load_scenario())["entities"] if item["id"] == 99)
        self.assertNotIn("controller", foreign)
