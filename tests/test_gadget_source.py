import json
from pathlib import Path
import re
import unittest


GAME = Path(__file__).parents[1] / "game" / "SynapseCommander.sdd"
GADGET = GAME / "luarules" / "gadgets" / "synapse_bridge.lua"
SCENARIO = GAME / "scenarios" / "rescue_smoke.json"
BASELINE = GAME / "scenarios" / "rescue_baseline.json"


class GadgetSourceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = GADGET.read_text(encoding="utf-8")

    def task_rate_check(self) -> int:
        """Index of the TASK branch's rate check (PRODUCE has its own, earlier)."""
        return self.source.index("if not use_token(teamID, frame)", self.source.index('if intent.type ~= "TASK"'))

    def test_stale_ids_survive_past_the_stale_threshold(self) -> None:
        self.assertIn("MAX_OBSERVATIONS = 0, 15, 128", self.source)
        self.assertIn("frame - observation_frame > 900", self.source)

    def test_backlog_is_drained_without_another_append(self) -> None:
        self.assertIn("if #partial == 0 and #chunk == 0 then return end", self.source)
        self.assertIn("while forwarded < 8 do", self.source)

    def test_outbox_has_no_unsupported_bridge_kind(self) -> None:
        self.assertNotIn('write("bridge",', self.source)
        self.assertIn('write("bridge_error",', self.source)

    def test_duplicate_is_not_replayed_as_accepted(self) -> None:
        self.assertIn('emit_receipt(intent, "rejected", "duplicate_intent_id")', self.source)

    def test_scenario_precedes_fixture_and_exposes_named_defs(self) -> None:
        self.assertIn("scenario_emitted = true", self.source)
        self.assertIn("if scenario_emitted and can_emit_effect", self.source)
        self.assertIn("definition and definition.name", self.source)

    def test_boundary_records_and_observation_history_are_chronological(self) -> None:
        self.assertIn("MAX_LINE + #PREFIX", self.source)
        self.assertIn("local order = observation_order[teamID] or {}", self.source)
        self.assertIn("table.remove(order, 1)", self.source)

    def test_pending_order_never_spends_a_rate_token(self) -> None:
        self.assertIn('emit_receipt(intent, "rejected", "unit_busy")', self.source)
        self.assertLess(
            self.source.index('emit_receipt(intent, "rejected", "unit_busy")'),
            self.task_rate_check(),
        )

    def test_synced_reducer_requires_the_public_envelope_schema(self) -> None:
        self.assertIn('intent.schema ~= "IntentEnvelope/v1"', self.source)

    def test_far_future_expiry_has_a_stable_rejection(self) -> None:
        self.assertIn('intent.expires_at_frame > frame + 900', self.source)
        self.assertIn('"expiry_out_of_range"', self.source)

    def test_visible_gaia_probe_stays_near_the_advance_route(self) -> None:
        self.assertIn('x + 200, Spring.GetGroundHeight(x + 200, z + 100), z + 100', self.source)

    def test_arrival_completes_a_move_without_waiting_for_engine_callback(self) -> None:
        self.assertIn("<= 16 * 16", self.source)
        self.assertIn("Physical arrival is still terminal", self.source)

    def test_already_arrived_rejects_before_rate_and_empty_queue_finishes(self) -> None:
        self.assertIn('emit_receipt(intent, "rejected", "already_at_waypoint")', self.source)
        self.assertIn('detail = { reason = "queue_empty" }', self.source)
        self.assertLess(
            self.source.index('emit_receipt(intent, "rejected", "already_at_waypoint")'),
            self.task_rate_check(),
        )


class FactoryProvisioningSourceTests(unittest.TestCase):
    """Loop 1 (rah26-8zp) contract for the synced reducer.

    No Lua interpreter is available on the development host, so these pin
    the source structure; a native run verifies behaviour.
    """

    def setUp(self) -> None:
        self.source = GADGET.read_text(encoding="utf-8")
        start = self.source.index("local function reduce_produce")
        self.produce = self.source[start:self.source.index("\n  end\n", start)]

    def order(self, *needles: str, within: str | None = None) -> None:
        text = self.produce if within is None else within
        positions = [text.index(needle) for needle in needles]
        self.assertEqual(positions, sorted(positions), needles)

    def test_produce_rejections_follow_the_agreed_order(self) -> None:
        self.order(
            '"invalid_payload"', '"unknown_factory"', '"unknown_chassis"', '"unknown_controller"',
            '"invalid_parameters"', "freshness_problem(intent, teamID, frame)", '"factory_queue_full"',
            '"budget_exhausted"', '"chip_unavailable"', '"rate_limited"',
        )

    def test_produce_spends_only_after_every_check_and_before_acceptance(self) -> None:
        self.order('"rate_limited"', "budgets.bodies_remaining = budgets.bodies_remaining - 1",
                   "budgets.chips[payload.controller] = budgets.chips[payload.controller] - 1",
                   'emit_receipt(intent, "accepted", nil)')
        self.assertIn("#jobs >= MAX_JOBS", self.produce)
        self.assertIn("MAX_JOBS = 16384, 8", self.source)

    def test_principal_and_embodiment_gate_every_scenario_intent(self) -> None:
        self.order('"wrong_team"', '"principal_unembodied"', 'intent.type == "PRODUCE"',
                   'if intent.type ~= "TASK"', '"not_owned"', '"not_embodied"', within=self.source)

    def test_produce_is_unsupported_without_a_scenario(self) -> None:
        self.assertIn('if scenario and intent.type == "PRODUCE" then return reduce_produce', self.source)

    def test_commander_binding_spends_a_body_and_its_chip(self) -> None:
        start = self.source.index("local function bind_commander")
        bind = self.source[start:self.source.index("\n  end\n", start)]
        self.assertIn("customParams.iscommander", bind)
        self.order('"commander_unbudgeted"', "budgets.bodies_remaining = budgets.bodies_remaining - 1",
                   "budgets.chips[chip] = budgets.chips[chip] - 1", 'effect("commander_bound"', within=bind)

    def test_job_effects_require_an_actual_unit(self) -> None:
        start = self.source.index("local function complete_job")
        complete = self.source[start:self.source.index("\n  end\n", start)]
        self.order("Spring.CreateUnit(", "if not unitID then", 'effect("factory_job_failed"', "return",
                   'effect("unit_created"', 'effect("factory_job_done"', within=complete)
        self.assertIn("controller = job.controller", complete)

    def test_job_units_have_one_unit_created_effect(self) -> None:
        self.order("job_spawning = job", "Spring.CreateUnit(", "job_spawning = nil", within=self.source)
        callin = self.source[self.source.index("function gadget:UnitCreated"):]
        self.order("if job_spawning then return end", '"unit_created"', within=callin)

    def test_factory_scenario_spawns_no_loop0_fixture(self) -> None:
        start = self.source.index("local function start_factory_scenario")
        body = self.source[start:self.source.index("\n  end\n", start)]
        self.assertNotIn('"armck"', body)
        self.assertIn('scenario_failed = true', body)
        game_start = self.source[self.source.index("function gadget:GameStart"):]
        self.order("start_factory_scenario(opts.synapse_scenario", "else", 'Spring.CreateUnit("armck"', within=game_start)

    def test_exhausted_choices_stay_permitted(self) -> None:
        self.assertIn("chassis = sorted_keys(scenario.chassis), controllers = sorted_keys(scenario.chips)", self.source)

    def test_gadget_reads_only_fields_the_shared_table_declares(self) -> None:
        table = json.loads(SCENARIO.read_text(encoding="utf-8"))
        self.assertEqual(table["schema"], "ScenarioTable/v1")
        self.assertIn('t.schema ~= "ScenarioTable/v1"', self.source)
        for field in ("factory", "bodies", "chips", "commander", "chassis", "waypoints"):
            self.assertIn(field, table)
            self.assertRegex(self.source, rf"\bt\.{field}\b")
        self.assertLessEqual(set(table["factory"]), {"name", "def", "offset"})
        self.assertIn(table["commander"]["chip"], table["chips"])
        for chassis in table["chassis"].values():
            self.assertEqual(set(chassis), {"def", "build_frames", "commission_frames"})
        self.assertLessEqual(len(SCENARIO.read_bytes()), 16384)
        self.assertTrue(re.fullmatch(r"[\w.-]{1,64}", table["version"]))


def function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    return source[start:source.index("\n  end\n", start)]


class InitialWorldSourceTests(unittest.TestCase):
    """rah26-5hf: predeployed assets, aged distress report, hidden truth."""

    def setUp(self) -> None:
        self.source = GADGET.read_text(encoding="utf-8")
        start = self.source.index("function gadget:GameFrame")
        self.observation = self.source[self.source.index('local id = "obs-0-" .. frame', start):self.source.index('out("observation", body)', start)]

    def test_hidden_truth_never_enters_the_observation(self) -> None:
        for secret in ("cause_hidden", "a.state", "a.contact", "a.position", "seed"):
            self.assertNotIn(secret, self.observation)
        # The roster reports identity and contact; status comes from contact or an observed loss.
        self.assertIn('status = live and "in_contact"', self.observation)
        self.assertIn('(a.known_lost and "lost" or "no_contact")', self.observation)
        self.assertIn("unit_id = live and a.unit_id or nil", self.observation)
        self.assertIn("age_frames = frame - r.observed_frame", self.observation)

    def test_truth_is_recorded_only_in_the_evaluator_record(self) -> None:
        record = function_body(self.source, "local function world_init_record")
        self.assertIn('name = "world_init"', record)
        self.assertIn("cause_hidden = d.cause_hidden", record)
        self.assertIn("inventory_consistent = consistent", record)
        self.assertIn('out("scenario", world_init_record(frame))', self.source)
        self.assertEqual(self.source.count("cause_hidden"), 3)  # validated once, copied into world_init; never observed

    def test_out_of_contact_units_are_never_reported_or_commandable(self) -> None:
        visible = function_body(self.source, "local function visible_source")
        self.assertTrue(visible.split("\n")[1].strip().startswith("if hidden[unitID] then return nil end"))
        self.assertIn("or hidden[unitID] then", self.source)
        callin = self.source[self.source.index("function gadget:UnitCreated"):]
        self.assertIn("if spawning_hidden then hidden[unitID] = true; return end", callin)
        spawn = function_body(self.source, "local function spawn_asset")
        self.assertIn("Spring.SetUnitSensorRadius(unitID, sensor, 0)", spawn)
        self.assertIn('HIDDEN_SENSORS = { "los", "airLos", "radar", "sonar", "seismic" }', self.source)

    def test_every_predeployed_asset_debits_the_production_budgets(self) -> None:
        spawn = function_body(self.source, "local function spawn_asset")
        self.order = [spawn.index(n) for n in (
            "budgets.bodies_remaining = budgets.bodies_remaining - 1",
            "budgets.chips[a.controller] = budgets.chips[a.controller] - 1",
            'if a.state ~= "active" then return end', "Spring.CreateUnit(")]
        self.assertEqual(self.order, sorted(self.order))
        self.assertIn('return nil, "inventory_insufficient"', self.source)

    def test_gadget_draws_no_randomness(self) -> None:
        self.assertNotRegex(self.source, r"math\.random|Spring\.GetRandom|RandomSeed")


class BaselineFixtureTests(unittest.TestCase):
    """The first initial-world fixture is internally consistent."""

    def setUp(self) -> None:
        self.table = json.loads(BASELINE.read_text(encoding="utf-8"))

    def test_inventory_covers_every_deployed_body_and_chip(self) -> None:
        t = self.table
        need_bodies = 1 + len(t["predeployed"])
        need_chips = {t["commander"]["chip"]: 1}
        for asset in t["predeployed"]:
            if asset["controller"] is not None:
                need_chips[asset["controller"]] = need_chips.get(asset["controller"], 0) + 1
        self.assertLessEqual(need_bodies, t["bodies"])
        for chip, n in need_chips.items():
            self.assertLessEqual(n, t["chips"][chip])
        # Expected start: 2 bodies and one fixed chip left for production.
        self.assertEqual(t["bodies"] - need_bodies, 2)
        self.assertEqual({c: t["chips"][c] - need_chips.get(c, 0) for c in t["chips"]},
                         {"reasoner_large_v1": 0, "reasoner_small_v1": 0, "escort_fixed_v1": 1})

    def test_assets_and_distress_are_constrained_data(self) -> None:
        t = self.table
        self.assertIsInstance(t["seed"], int)
        names = [a["name"] for a in t["predeployed"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertLessEqual(len(names), 8)
        for asset in t["predeployed"]:
            self.assertIn(asset["state"], {"active", "destroyed"})
            self.assertIsInstance(asset["contact"], bool)
            self.assertTrue(asset["controller"] is None or asset["controller"] in t["chips"])
            self.assertTrue(0 < asset.get("health", 1) <= 1)
        distress = t["distress"]
        self.assertIn(distress["asset"], names)
        # Cause and target state are separate fields; the report's position is
        # last known, not the asset's true position.
        target = next(a for a in t["predeployed"] if a["name"] == distress["asset"])
        self.assertNotEqual(distress["last_known"]["position"], target["position"])
        self.assertNotEqual(distress["cause_hidden"], target["state"])

    def test_a_destroyed_asset_is_out_of_contact(self) -> None:
        # A destroyed asset the team was in contact with would be a known loss;
        # this fixture's destroyed hauler is not, so the team cannot know it is gone.
        for asset in self.table["predeployed"]:
            if asset["state"] == "destroyed":
                self.assertFalse(asset["contact"])


if __name__ == "__main__":
    unittest.main()
