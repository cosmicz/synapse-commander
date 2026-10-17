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
    def test_principal_distress_accepts_engine_delivery_metadata_without_forwarding_it(self):
        source = engine_observation()
        body = {"id": 42, "pos": [100, 0, 100], "source": "own_body"}
        source.update({
            "principal": "commander", "body": body, "entities": [body],
            "scenario": {"id": "view_smoke", "version": "1-provisional", "waypoints": {"W1": [1, 0, 1]}},
            "reports": [{"kind": "distress", "asset": "M1", "position": [5, 0, 6], "observed_frame": -900,
                         "delivered_frame": 0, "age_frames": 900, "symptom": "no_heartbeat"}],
        })
        observed = bounded_observation(source, scenario_table=load_scenario("view_smoke"), principal="commander")
        self.assertEqual(observed["reports"], [{"kind": "distress", "asset": "M1", "position": [5, 0, 6],
                                                   "observed_frame": -900, "age_frames": 900,
                                                   "symptom": "no_heartbeat"}])
        self.assertNotIn("delivered_frame", observed["reports"][0])

    def test_commander_keeps_only_declared_rescue_objective_and_extraction(self):
        source = engine_observation()
        body = {"id": 42, "pos": [100, 0, 100], "source": "own_body"}
        source.update({
            "principal": "commander", "body": body, "entities": [body],
            "scenario": {"waypoints": {"W1": [1, 0, 1]},
                         "extraction": {"id": "extraction_v1", "position": [200, 300], "radius": 160,
                                        "private": "not forwarded"}},
            "objective": {"type": "rescue_return", "target": "M1", "status": "pending", "hidden": True},
            "permitted_intents": [{"type": "TASK", "verbs": ["advance", "move_to", "raw_engine"]}],
        })
        # The public source must be exact; an extra objective field is never
        # treated as a harmless pass-through.
        with self.assertRaisesRegex(ProtocolError, "commander objective"):
            bounded_observation(source, scenario_table=load_scenario("rescue_baseline"), principal="commander")
        source["objective"].pop("hidden")
        source["scenario"]["extraction"].pop("private")
        observed = bounded_observation(source, scenario_table=load_scenario("rescue_baseline"), principal="commander")
        self.assertEqual(observed["objective"], {"type": "rescue_return", "target": "M1", "status": "pending"})
        self.assertEqual(observed["scenario"]["extraction"], {"id": "extraction_v1", "position": [200, 300], "radius": 160})
        self.assertEqual(observed["permitted_intents"], [{"type": "TASK", "verbs": ["advance", "move_to"]}])

    def test_roster_hides_disconnected_identity_and_preserves_historical_distress(self):
        source = engine_observation()
        source.update({
            "scenario": {"id": "rescue_baseline", "version": "1", "waypoints": {"W1": [1, 0, 1]}},
            "roster": [{"name": "M1", "def": "armflea", "controller": "reasoner_small_v1", "status": "no_contact"}],
            "reports": [{"kind": "distress", "asset": "M1", "position": [5, 0, 6], "observed_frame": -30, "age_frames": 30, "symptom": "silent"}],
            "world_init": {"cause_hidden": "ambush", "unit_id": 999},
        })
        table = {"id": "rescue_baseline", "version": "1", "chips": {"reasoner_small_v1": 1}, "predeployed": [{"name": "M1", "def": "armflea", "controller": "reasoner_small_v1"}], "distress": {"symptom": "silent"}}
        observed = bounded_observation(source, scenario_table=table)
        self.assertEqual(observed["roster"], [{"name": "M1", "def": "armflea", "controller": "reasoner_small_v1", "status": "no_contact"}])
        self.assertEqual(observed["reports"][0]["observed_frame"], -30)
        self.assertNotIn("world_init", observed)
        self.assertNotIn("ambush", json.dumps(observed))

    def test_expedition_requires_scenario_and_never_attaches_identity_to_no_contact(self):
        source = engine_observation()
        source.update({
            "scenario": {"id": "rescue_baseline", "version": "1"},
            "roster": [{"name": "M1", "def": "armflea", "controller": None, "status": "no_contact"}],
            "reports": [{"kind": "distress", "asset": "M1", "position": [5, 0, 6], "observed_frame": -1, "age_frames": 1, "symptom": "silent"}],
        })
        table = {"id": "rescue_baseline", "version": "1", "chips": {}, "predeployed": [{"name": "M1", "def": "armflea", "controller": None}], "distress": {"symptom": "silent"}}
        with self.assertRaisesRegex(ProtocolError, "selected scenario"):
            bounded_observation(source)
        source["roster"][0]["unit_id"] = 99
        with self.assertRaisesRegex(ProtocolError, "invalid roster entry"):
            bounded_observation(source, scenario_table=table)
        source["roster"][0].pop("unit_id")
        source["reports"][0]["age_frames"] = -1
        with self.assertRaisesRegex(ProtocolError, "bounded non-negative"):
            bounded_observation(source, scenario_table=table)

    def test_checked_in_rescue_baseline_projects_only_declared_public_expedition_data(self):
        source = engine_observation()
        source.update({
            "scenario": {"id": "rescue_baseline", "version": load_scenario("rescue_baseline")["version"], "waypoints": {"W1": [500, 0, 100]}},
            "roster": [
                {"name": "S0", "def": "armflea", "controller": "escort_fixed_v1", "status": "in_contact", "unit_id": 71},
                {"name": "M1", "def": "armck", "controller": "reasoner_small_v1", "status": "no_contact"},
                {"name": "H0", "def": "armck", "controller": "escort_fixed_v1", "status": "no_contact"},
            ],
            "reports": [{"kind": "distress", "asset": "M1", "position": [2400, 0, 1300], "observed_frame": -900, "age_frames": 900, "symptom": "no_heartbeat"}],
            "world_init": {"seed": 1017, "cause_hidden": load_scenario("rescue_baseline")["distress"]["cause_hidden"], "unit_ids": {"M1": 991}},
        })
        observed = bounded_observation(source, scenario_table=load_scenario("rescue_baseline"))
        self.assertEqual(observed["roster"][0], {"name": "H0", "def": "armck", "controller": "escort_fixed_v1", "status": "no_contact"})
        self.assertEqual(observed["roster"][-1], {"name": "S0", "def": "armflea", "controller": "escort_fixed_v1", "status": "in_contact", "unit_id": 71})
        self.assertEqual(observed["reports"][0]["observed_frame"], -900)
        public = json.dumps(observed)
        self.assertNotIn("world_init", public)
        self.assertNotIn(load_scenario("rescue_baseline")["distress"]["cause_hidden"], public)
        self.assertNotIn("991", public)

    def test_http_gateway_uses_selected_table_for_rescue_baseline_projection(self):
        source = engine_observation()
        source.update({
            "scenario": {"id": "rescue_baseline", "version": load_scenario("rescue_baseline")["version"], "waypoints": {"W1": [500, 0, 100]}},
            "roster": [{"name": "M1", "def": "armck", "controller": "reasoner_small_v1", "status": "no_contact"}],
            "reports": [{"kind": "distress", "asset": "M1", "position": [2400, 0, 1300], "observed_frame": -900, "age_frames": 900, "symptom": "no_heartbeat"}],
            "world_init": {"cause_hidden": load_scenario("rescue_baseline")["distress"]["cause_hidden"]},
        })
        transport = MemoryTransport(frame=15)
        transport.publish_observation("0", source)
        server = LoopbackGateway(transport, principal="smoke", team="0", scenario="rescue_baseline").make_server()
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            connection.request("GET", "/v1/observation")
            response = connection.getresponse()
            observed = json.loads(response.read())
            self.assertEqual(response.status, 200)
            self.assertEqual(observed["roster"][0]["status"], "no_contact")
            self.assertNotIn("unit_id", observed["roster"][0])
            self.assertEqual(observed["reports"][0]["observed_frame"], -900)
            self.assertNotIn("world_init", observed)
        finally:
            connection.close()
            server.shutdown()
            thread.join()
            server.server_close()

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

    def test_missing_factory_identity_is_a_public_absent_condition(self):
        source = provisioning_observation()
        source["factories"][0]["unit_id"] = None
        observed = bounded_observation(source, scenario_table=load_scenario())
        self.assertIsNone(observed["factories"][0]["unit_id"])

    def test_principal_factory_identity_may_omit_its_private_job_queue(self):
        source = provisioning_observation()
        source["factories"] = [{"name": "F0", "unit_id": 7}]
        observed = bounded_observation(source, scenario_table=load_scenario())
        self.assertEqual(observed["factories"], [{"name": "F0", "unit_id": 7, "jobs": []}])

    def test_jobs_are_ordered_by_start_frame(self):
        source = provisioning_observation()
        first = dict(source["factories"][0]["jobs"][0], job_id="later", started_frame=9)
        second = dict(source["factories"][0]["jobs"][0], job_id="earlier", started_frame=3)
        source["factories"][0]["jobs"] = [first, second]
        observed = bounded_observation(source, scenario_table=load_scenario())
        self.assertEqual([job["job_id"] for job in observed["factories"][0]["jobs"]], ["earlier", "later"])

    def test_foreign_entity_controller_is_not_exposed(self):
        source = provisioning_observation()
        source["entities"].append({"id": 99, "pos": [1, 0, 1], "source": "los", "controller": "reasoner_large_v1"})
        foreign = next(item for item in bounded_observation(source, scenario_table=load_scenario())["entities"] if item["id"] == 99)
        self.assertNotIn("controller", foreign)

    def test_entity_projection_drops_unselected_controller_and_unknown_fields(self):
        source = engine_observation()
        source["entities"][0].update({"controller": "secret", "private_target": [99, 0, 99]})
        observed = bounded_observation(source)
        self.assertNotIn("controller", observed["entities"][0])
        self.assertNotIn("private_target", observed["entities"][0])

    def test_loop0_entity_keeps_typed_public_telemetry_while_filtering_unknown_keys(self):
        source = engine_observation()
        source["entities"] = [{
            "id": 42, "def": "armck", "pos": [100, 0, 100], "source": "own_unit", "team": 0,
            "health": 750.5, "max_health": 1000, "commands": 2, "observed_frame": 15,
            "age_frames": 0, "controller": "not_public_in_loop0", "private_plan": "secret",
        }]
        observed = bounded_observation(source)
        self.assertEqual(observed["entities"], [{
            "id": 42, "def": "armck", "pos": [100, 0, 100], "source": "own_unit", "team": 0,
            "health": 750.5, "max_health": 1000, "commands": 2, "observed_frame": 15,
            "age_frames": 0,
        }])

    def test_roster_names_are_unique(self):
        source = engine_observation()
        source.update({
            "scenario": {"id": "rescue_baseline", "version": load_scenario("rescue_baseline")["version"]},
            "roster": [
                {"name": "M1", "def": "armck", "controller": "reasoner_small_v1", "status": "no_contact"},
                {"name": "M1", "def": "armck", "controller": "reasoner_small_v1", "status": "no_contact"},
            ],
        })
        with self.assertRaisesRegex(ProtocolError, "duplicate roster"):
            bounded_observation(source, scenario_table=load_scenario("rescue_baseline"))

    def test_produce_permissions_reject_unknown_ids_and_dedupe_task_advance(self):
        source = provisioning_observation()
        source["permitted_intents"].insert(0, {"type": "TASK", "verbs": ["advance"]})
        observed = bounded_observation(source, scenario_table=load_scenario())
        self.assertEqual(sum(item["type"] == "TASK" for item in observed["permitted_intents"]), 1)
        source["permitted_intents"][2]["chassis"] = ["private_chassis"]
        with self.assertRaisesRegex(ProtocolError, "unknown PRODUCE permission"):
            bounded_observation(source, scenario_table=load_scenario())
