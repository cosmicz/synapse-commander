"""The gadget and gateway read the same bounded public scenario declaration."""
import copy
import unittest

from synapse_commander.scenarios import DEFAULT_SCENARIO, SCENARIO_SCHEMA, ScenarioError, _validate, load_scenario, supported_chassis, supported_controllers


class ScenarioTableTests(unittest.TestCase):
    def test_demo_variants_preserve_surveyed_geometry_and_fund_one_child(self):
        baseline = load_scenario("rescue_baseline")
        for name, spare_bodies, spare_fixed, deadline in (
            ("rescue_last_chip", 1, 0, 12000), ("rescue_short_window", 2, 1, 9000),
        ):
            with self.subTest(name=name):
                table = load_scenario(name)
                # Lost assets consumed their body/chip too; only the residual funds production.
                used = {chip: 0 for chip in table["chips"]}
                used[table["commander"]["chip"]] += 1
                for asset in table["predeployed"]:
                    used[asset["controller"]] += 1
                self.assertEqual(table["bodies"] - 1 - len(table["predeployed"]), spare_bodies)
                self.assertEqual({chip: count - used[chip] for chip, count in table["chips"].items()},
                                 {"reasoner_large_v1": 0, "reasoner_small_v1": 1, "escort_fixed_v1": spare_fixed})
                self.assertEqual(table["mission"]["timeout_frames"], deadline)
                for key in ("factory", "commander", "predeployed", "distress", "waypoints", "comms",
                            "extraction_zones", "principals", "reasoner_chips", "sensors", "chassis"):
                    self.assertEqual(table[key], baseline[key], key)

    def test_public_briefing_has_a_bounded_exact_shape(self):
        for briefing in (None, {}, {"title": "ok", "text": "ok", "cause": "secret"},
                         {"title": "x" * 81, "text": "ok"}, {"title": "ok", "text": "x" * 601},
                         {"title": "", "text": "ok"}, {"title": "ok", "text": "bad\x00"}):
            table = copy.deepcopy(load_scenario("rescue_last_chip"))
            table["briefing"] = briefing
            with self.subTest(briefing=briefing):
                self.assertRaises(ScenarioError, _validate, table, table["id"])

    def test_rescue_smoke_table_is_versioned_and_has_real_engine_definitions(self):
        table = load_scenario()
        self.assertEqual((table["schema"], table["id"], table["version"]), (SCENARIO_SCHEMA, DEFAULT_SCENARIO, "1-provisional"))
        self.assertEqual(table["factory"]["def"], "armlab")
        self.assertEqual(table["chassis"]["scout_v1"]["def"], "armflea")
        self.assertEqual(table["chassis"]["carrier_v1"]["def"], "armck")
        self.assertEqual(table["chassis"]["relay_support_v1"]["def"], "armmark")
        self.assertEqual(table["bodies"], 4)
        self.assertEqual(set(table["chips"]), supported_controllers())
        self.assertEqual(set(table["chassis"]), supported_chassis())

    def test_optional_scenario_fields_are_ignored_by_the_public_table_reader(self):
        table = dict(load_scenario())
        table["predeployed"] = {"private": True}
        table["distress"] = {"hidden": True}
        self.assertEqual(_validate(table, DEFAULT_SCENARIO)["id"], DEFAULT_SCENARIO)

    def test_engine_only_commander_placement_is_checked_but_publicly_compatible(self):
        table = load_scenario("rescue_baseline")
        self.assertEqual(table["commander"]["position"], [0, 240])

    def test_v2_parts_entrapment_is_explicit_and_towing_is_refused(self):
        table = copy.deepcopy(load_scenario("rescue_baseline"))
        table["resources"] = {"parts": 1}
        table["commander"]["speed_factor"] = .6
        table["chassis"]["carrier_v1"].update({"speed_factor": .6, "carries_parts": True})
        table["entrapment"] = {
            "asset": "M1", "until": "parts_delivered", "assist_capability": "assist",
            "assist_range": 96, "assist_frames": 150,
            "parts_carrier_chassis": ["carrier_v1"],
        }
        self.assertEqual(_validate(table, table["id"])["entrapment"]["until"], "parts_delivered")
        invalid = copy.deepcopy(table)
        invalid["entrapment"]["until"] = "towed"
        with self.assertRaisesRegex(ScenarioError, "entrapment"):
            _validate(invalid, invalid["id"])
        invalid = copy.deepcopy(table)
        invalid["chassis"]["carrier_v1"]["carries_parts"] = False
        with self.assertRaisesRegex(ScenarioError, "entrapment"):
            _validate(invalid, invalid["id"])
        invalid = copy.deepcopy(table)
        invalid["commander"]["speed_factor"] = .01
        with self.assertRaisesRegex(ScenarioError, "invalid commander"):
            _validate(invalid, invalid["id"])

    def test_view_smoke_is_a_separate_principal_fixture(self):
        table = load_scenario("view_smoke")
        self.assertEqual(set(table["principals"]), {"commander", "M1"})
        self.assertEqual(table["predeployed"][1]["name"], "R0")

    def test_rescue_baseline_declares_the_kbk_min_extraction_and_timeout(self):
        table = load_scenario("rescue_baseline")
        self.assertEqual(table["version"], "6-provisional")
        # One spare reasoning chip so the commander can manufacture a subordinate mind.
        self.assertEqual(table["chips"], {"reasoner_large_v1": 1, "reasoner_small_v1": 2, "escort_fixed_v1": 3})
        self.assertEqual(table["extraction_zones"], {"extraction_v1": {"position": [-752, 896], "radius": 160, "dwell_frames": 150}})
        self.assertEqual(table["mission"], {"timeout_frames": 12000})
        self.assertEqual(table["objective"], {"type": "rescue_return", "target": "M1", "extraction": "extraction_v1"})

    def test_extraction_zones_mission_and_objective_bindings_fail_closed(self):
        def broken(change):
            table = copy.deepcopy(load_scenario("rescue_baseline"))
            change(table)
            return table
        zone = lambda t: t["extraction_zones"]["extraction_v1"]
        for name, change in {
            "empty zones": lambda t: t.__setitem__("extraction_zones", {}),
            "zones not an object": lambda t: t.__setitem__("extraction_zones", []),
            "bad zone name": lambda t: t.__setitem__("extraction_zones", {"x y": zone(t)}),
            "short position": lambda t: zone(t).__setitem__("position", [1]),
            "infinite position": lambda t: zone(t).__setitem__("position", [float("inf"), 0]),
            "zero radius": lambda t: zone(t).__setitem__("radius", 0),
            "boolean radius": lambda t: zone(t).__setitem__("radius", True),
            "zero dwell": lambda t: zone(t).__setitem__("dwell_frames", 0),
            "fractional dwell": lambda t: zone(t).__setitem__("dwell_frames", 1.5),
            "extra zone key": lambda t: zone(t).__setitem__("hidden", 1),
            "undeclared zone": lambda t: t["objective"].__setitem__("extraction", "extraction_v2"),
            "target not a principal": lambda t: t["objective"].__setitem__("target", "H0"),
            "no principals": lambda t: t.pop("principals"),
            "zero timeout": lambda t: t.__setitem__("mission", {"timeout_frames": 0}),
            "string timeout": lambda t: t.__setitem__("mission", {"timeout_frames": "12000"}),
            "extra mission key": lambda t: t["mission"].__setitem__("result", "extracted"),
            "mission not an object": lambda t: t.__setitem__("mission", 12000),
        }.items():
            with self.subTest(name):
                self.assertRaises(ScenarioError, _validate, broken(change), "rescue_baseline")

    def test_tables_without_extraction_zones_keep_their_placeholder_targets(self):
        for name in ("rescue_smoke", "view_smoke"):
            with self.subTest(name):
                table = load_scenario(name)
                self.assertNotIn("extraction_zones", table)
                self.assertEqual(table["objective"]["target"], "rescue_target_v1")

    def test_rescue_v2_declares_slow_bodies_parts_and_entrapment(self):
        # kbk Design 9 f: baseline geometry plus speed factors, one part and a parts-delivery entrapment.
        table = load_scenario("rescue_v2")
        self.assertEqual((table["id"], table["version"]), ("rescue_v2", "1"))
        self.assertEqual({n: c.get("speed_factor") for n, c in table["chassis"].items()},
                         {"scout_v1": 0.8, "carrier_v1": 0.6, "relay_support_v1": 0.6})
        self.assertEqual(table["commander"]["speed_factor"], 0.6)
        m1 = next(a for a in table["predeployed"] if a["name"] == "M1")
        self.assertEqual((m1["speed_factor"], m1["health"], m1["position"]), (0.5, 0.4, [-1536, 2176]))
        self.assertIs(table["chassis"]["carrier_v1"]["carries_parts"], True)
        self.assertEqual(table["resources"], {"parts": 1})
        self.assertEqual(table["entrapment"], {"asset": "M1", "until": "parts_delivered", "assist_capability": "assist",
                                               "assist_range": 96, "assist_frames": 150,
                                               "parts_carrier_chassis": ["carrier_v1"]})
        # 18:10 ruling: only a produced carrier carries the one part; the relay does not.
        self.assertNotIn("carries_parts", table["chassis"]["relay_support_v1"])
        self.assertEqual(table["mission"], {"timeout_frames": 15000})
        self.assertEqual(table["chips"], load_scenario("rescue_baseline")["chips"])
        self.assertEqual(table["distress"]["cause_hidden"], "terrain_entrapment+parts")

    def test_mission_v2_fields_fail_closed(self):
        def broken(change):
            table = copy.deepcopy(load_scenario("rescue_v2"))
            change(table)
            return table
        trap = lambda t: t["entrapment"]
        for name, change in {
            "slow chassis": lambda t: t["chassis"]["scout_v1"].__setitem__("speed_factor", 0.01),
            "fast chassis": lambda t: t["chassis"]["scout_v1"].__setitem__("speed_factor", 1.5),
            "boolean speed": lambda t: t["chassis"]["scout_v1"].__setitem__("speed_factor", True),
            "commander speed": lambda t: t["commander"].__setitem__("speed_factor", 0),
            "asset speed": lambda t: t["predeployed"][1].__setitem__("speed_factor", float("nan")),
            "carries parts not bool": lambda t: t["chassis"]["carrier_v1"].__setitem__("carries_parts", 1),
            "negative parts": lambda t: t.__setitem__("resources", {"parts": -1}),
            "unknown resource": lambda t: t.__setitem__("resources", {"parts": 1, "gold": 2}),
            "towing reserved": lambda t: trap(t).__setitem__("until", "towed"),
            "unknown asset": lambda t: trap(t).__setitem__("asset", "M9"),
            "destroyed asset": lambda t: trap(t).__setitem__("asset", "H0"),
            "zero range": lambda t: trap(t).__setitem__("assist_range", 0),
            "zero frames": lambda t: trap(t).__setitem__("assist_frames", 0),
            "undeclared carrier": lambda t: trap(t).__setitem__("parts_carrier_chassis", ["truck_v1"]),
            "no carriers": lambda t: trap(t).__setitem__("parts_carrier_chassis", []),
            "extra entrapment key": lambda t: trap(t).__setitem__("towed_by", "S0"),
        }.items():
            with self.subTest(name):
                self.assertRaises(ScenarioError, _validate, broken(change), "rescue_v2")


if __name__ == "__main__":
    unittest.main()
