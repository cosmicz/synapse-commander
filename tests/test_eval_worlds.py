"""Seeded evaluation worlds (rah26-4j3)."""

from __future__ import annotations

import copy
import json
import unittest
from collections import Counter

from synapse_commander import eval_worlds as ew
from synapse_commander.scenarios import ScenarioError, load_scenario


def world(state: str) -> tuple[dict, dict]:
    return ew.generate(ew.rehearsal_seed(state))


def assets(table: dict) -> dict:
    return {a["name"]: a for a in table["predeployed"]}


class Reproducibility(unittest.TestCase):
    def test_same_seed_gives_identical_bytes(self) -> None:
        for seed in (1, 2, 3, 77, 2 ** 31 - 1):
            with self.subTest(seed=seed):
                first, second = ew.generate(seed), ew.generate(seed)
                self.assertEqual([ew.canonical(x) for x in first], [ew.canonical(x) for x in second])

    def test_checked_in_rehearsal_set_regenerates_byte_for_byte(self) -> None:
        worlds = ew.rehearsal_worlds()
        self.assertEqual(sorted(t["target_state"] for _, _, t in worlds), sorted(ew.TARGET_STATES))
        for seed, table, truth in worlds:
            table_path, truth_path = ew.rehearsal_paths(seed)
            with self.subTest(seed=seed):
                self.assertEqual(table_path.read_text(encoding="utf-8"), ew.canonical(table))
                self.assertEqual(truth_path.read_text(encoding="utf-8"), ew.canonical(truth))
                self.assertFalse(truth["forced_state"])
                self.assertEqual(truth["table_sha256"], ew.table_sha256(json.loads(table_path.read_text())))

    def test_changed_seeds_change_the_world_and_cover_every_state(self) -> None:
        tables = {seed: ew.canonical(ew.generate(seed)[0]) for seed in range(1, 41)}
        self.assertEqual(len(set(tables.values())), len(tables))
        counts = Counter(ew.sample_state(seed) for seed in range(1, 4001))
        self.assertEqual(set(counts), set(ew.TARGET_STATES))
        # Declared 2:1:1 weights, within sampling noise.
        self.assertAlmostEqual(counts["present_stranded"] / 4000, 0.5, delta=0.03)
        self.assertAlmostEqual(counts["never_deployed"] / 4000, 0.25, delta=0.03)

    def test_seed_must_be_a_bounded_integer(self) -> None:
        for seed in (-1, 2 ** 31, True, "1", 1.5):
            with self.subTest(seed=seed), self.assertRaises(ew.EvalWorldError):
                ew.generate(seed)


class TargetStates(unittest.TestCase):
    def test_inventory_is_conserved_in_every_state(self) -> None:
        for state in ew.TARGET_STATES:
            table, truth = world(state)
            with self.subTest(state=state):
                bodies = truth["inventory"]["bodies"]
                self.assertEqual(bodies["total"], bodies["deployed"] + bodies["lost_before_run"] + bodies["available"])
                for chip, c in truth["inventory"]["chips"].items():
                    self.assertEqual(c["total"], c["deployed"] + c["lost_before_run"] + c["available"], chip)
                    self.assertGreaterEqual(c["available"], 0)

    def test_stranded_mind_is_a_live_impaired_principal_with_an_aged_report(self) -> None:
        table, truth = world("present_stranded")
        m1 = assets(table)["M1"]
        self.assertEqual((m1["state"], m1["contact"], m1["controller"]), ("active", False, "reasoner_small_v1"))
        self.assertIn(m1["health"], ew.STRANDED_HEALTH)
        self.assertEqual(set(table["principals"]), {"commander", "M1"})
        self.assertEqual(table["distress"]["asset"], "M1")
        self.assertIn(table["distress"]["last_known"]["age_frames"], ew.DISTRESS_AGES)
        self.assertEqual(table["objective"]["target"], "M1")

    def test_destroyed_mind_has_no_principal_and_only_its_old_packet(self) -> None:
        table, truth = world("destroyed_before_run")
        m1 = assets(table)["M1"]
        self.assertEqual(m1["state"], "destroyed")
        self.assertEqual(set(table["principals"]), {"commander"})
        age = table["distress"]["last_known"]["age_frames"]
        self.assertTrue(-age < truth["destroyed_at_frame"] < 0)
        self.assertEqual(truth["inventory"]["chips"]["reasoner_small_v1"]["lost_before_run"], 1)

    def test_never_deployed_invents_no_mind_and_keeps_the_unused_chip(self) -> None:
        table, truth = world("never_deployed")
        self.assertNotIn("M1", assets(table))
        self.assertEqual(set(table["principals"]), {"commander"})
        alarm = assets(table)[table["distress"]["asset"]]
        self.assertEqual((alarm["name"], alarm["controller"]), ("E1", "escort_fixed_v1"))
        self.assertIsNone(truth["target"])
        self.assertEqual(truth["inventory"]["chips"]["reasoner_small_v1"]["available"], 1)
        # A different spare inventory is recorded, not equalised.
        self.assertEqual(truth["inventory"]["chips"]["escort_fixed_v1"]["available"], 0)

    def test_every_body_has_a_real_controller_and_known_deployments_stay_visible(self) -> None:
        for state in ew.TARGET_STATES:
            table, _ = world(state)
            with self.subTest(state=state):
                self.assertTrue(all(a["controller"] for a in table["predeployed"]))
                self.assertIn("H0", assets(table))


class InvalidCombinations(unittest.TestCase):
    def refused(self, state: str, mutate) -> None:
        table, truth = world(state)
        table, truth = copy.deepcopy(table), copy.deepcopy(truth)
        mutate(table, truth)
        truth["table_sha256"] = ew.table_sha256(table)
        if "inventory" in truth and not truth.get("_keep_inventory"):
            truth["inventory"] = ew.inventory(table)
        truth.pop("_keep_inventory", None)
        with self.assertRaises(ew.EvalWorldError):
            ew.validate(table, truth)

    def test_impossible_principals(self) -> None:
        self.refused("destroyed_before_run", lambda t, _: t["principals"].__setitem__("M1", {"chip": "reasoner_small_v1"}))
        self.refused("present_stranded", lambda t, _: t["principals"].pop("M1"))
        self.refused("never_deployed", lambda t, _: t["principals"].__setitem__("E1", {"chip": "escort_fixed_v1"}))

    def test_a_principal_must_carry_its_bodys_reasoner_chip(self) -> None:
        self.refused("present_stranded", lambda t, _: t["principals"]["M1"].__setitem__("chip", "reasoner_large_v1"))

    def test_never_deployed_cannot_rename_its_bot_a_mind(self) -> None:
        def rename(t, _):
            assets(t)["E1"]["name"] = "M1"
            t["distress"]["asset"] = "M1"
            t["objective"]["target"] = "M1"
        self.refused("never_deployed", rename)

    def test_never_deployed_cannot_name_a_mind(self) -> None:
        def add_m1(t, _):
            t["predeployed"].append({"name": "M1", "def": "armck", "controller": "reasoner_small_v1",
                                     "position": list(ew.ANCHORS["target"]), "state": "destroyed", "contact": False})
        self.refused("never_deployed", add_m1)

    def test_inventory_cannot_be_overdrawn(self) -> None:
        self.refused("present_stranded", lambda t, _: t.__setitem__("bodies", 3))
        self.refused("present_stranded", lambda t, _: t["chips"].__setitem__("escort_fixed_v1", 1))

    def test_geometry_and_links_follow_the_measured_anchors(self) -> None:
        self.refused("present_stranded", lambda t, _: assets(t)["M1"].__setitem__("position", [-1500, 2176]))
        self.refused("present_stranded", lambda t, _: assets(t)["M1"].__setitem__("contact", True))
        self.refused("present_stranded", lambda t, _: assets(t)["S0"].__setitem__("contact", False))
        self.refused("present_stranded", lambda t, _: t["commander"].__setitem__("position", [0, 0]))

    def test_roles_cannot_swap_measured_anchors(self) -> None:
        # Each position is an anchor, but in the wrong role, so the link facts no longer hold.
        target, factory = list(ew.ANCHORS["target"]), list(ew.ANCHORS["factory"])
        self.refused("present_stranded", lambda t, _: t["factory"].__setitem__("offset", target))
        self.refused("present_stranded", lambda t, _: t["extraction_zones"]["extraction_v1"].__setitem__("position", factory))
        self.refused("present_stranded", lambda t, _: assets(t)["S0"].__setitem__("position", list(ew.ANCHORS["H0"])))
        self.refused("present_stranded", lambda t, _: t["distress"]["last_known"].__setitem__("position", target))

        def swap_waypoints(t, _):
            t["waypoints"]["W1"], t["waypoints"]["W3"] = t["waypoints"]["W3"], t["waypoints"]["W1"]
        self.refused("present_stranded", swap_waypoints)
        self.refused("present_stranded", lambda t, _: t["waypoints"].pop("W2"))
        self.refused("present_stranded", lambda t, _: t["waypoints"].__setitem__("W4", [-512, 0, 1536]))

    def test_comms_must_match_the_measured_links(self) -> None:
        self.refused("present_stranded", lambda t, _: t["comms"].__setitem__("range", 2000))
        self.refused("present_stranded", lambda t, _: t["comms"].__setitem__("relay_chassis", ["scout_v1"]))

    def test_distress_needs_a_valid_source_and_age(self) -> None:
        self.refused("present_stranded", lambda t, _: t["distress"].__setitem__("asset", "Z9"))
        self.refused("destroyed_before_run", lambda _, tr: tr.__setitem__("destroyed_at_frame", -5000))
        self.refused("destroyed_before_run", lambda _, tr: tr.__setitem__("destroyed_at_frame", 10))
        self.refused("never_deployed", lambda t, _: assets(t)["E1"].__setitem__("controller", "reasoner_small_v1"))

    def test_truth_must_describe_the_table(self) -> None:
        table, truth = world("present_stranded")
        self.assertRaises(ew.EvalWorldError, ew.validate, table, dict(truth, table_sha256="0" * 64))
        self.assertRaises(ew.EvalWorldError, ew.validate, table, dict(truth, seed=truth["seed"] + 1))
        self.assertRaises(ew.EvalWorldError, ew.validate, table, dict(truth, cause="enemy_capture"))
        self.assertRaises(ew.EvalWorldError, ew.validate, table, dict(truth, target_state="storm"))
        self.assertRaises(ew.EvalWorldError, ew.validate, table,
                          dict(truth, inventory=dict(truth["inventory"], bodies={**truth["inventory"]["bodies"], "available": 3})))


class HiddenTruth(unittest.TestCase):
    def test_tables_carry_no_evaluator_labels(self) -> None:
        for state in ew.TARGET_STATES:
            table, truth = world(state)
            text = json.dumps(table)
            with self.subTest(state=state):
                for label in ew.TARGET_STATES + ("sampled_state", "forced_state", "destroyed_at_frame", "target_state"):
                    self.assertNotIn(label, text)
                # The cause is engine-internal truth, present only as distress.cause_hidden.
                self.assertEqual(text.count(truth["cause"]), 1)
                self.assertEqual((table["id"], table["version"]), (ew.FAMILY_ID, ew.FAMILY_VERSION))

    def test_a_public_field_naming_the_state_is_refused(self) -> None:
        table, truth = world("never_deployed")
        table["objective"]["type"] = "never_deployed"
        truth = dict(truth, table_sha256=ew.table_sha256(table))
        self.assertRaises(ew.EvalWorldError, ew.validate, table, truth)

    def test_portable_artifacts_carry_no_rig_provenance(self) -> None:
        # Native evidence ids and rig commits are pinned on the evaluator side, not in the app.
        self.assertEqual(set(ew.SOURCE_PINS), {"fixture", "fixture_commit", "fixture_sha256", "geometry_sha256"})
        for seed, table, truth in ew.rehearsal_worlds():
            with self.subTest(seed=seed):
                self.assertNotIn("placement", json.dumps(truth))
                self.assertNotIn("placement", json.dumps(table))

    def test_rehearsal_truth_is_outside_the_game_archive(self) -> None:
        for seed, _, _ in ew.rehearsal_worlds():
            table_path, truth_path = ew.rehearsal_paths(seed)
            self.assertIn("SynapseCommander.sdd", table_path.parts)
            self.assertNotIn("SynapseCommander.sdd", truth_path.parts)

    def test_eval_tables_are_not_selectable_by_the_scenario_loader(self) -> None:
        # Storage for rehearsal and offline reports, not yet a runnable --scenario.
        with self.assertRaises(ScenarioError):
            load_scenario("eval/rehearsal-0001")


if __name__ == "__main__":
    unittest.main()
