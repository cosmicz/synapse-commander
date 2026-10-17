"""The gadget and gateway read the same bounded public scenario declaration."""
import copy
import unittest

from synapse_commander.scenarios import DEFAULT_SCENARIO, SCENARIO_SCHEMA, ScenarioError, _validate, load_scenario, supported_chassis, supported_controllers


class ScenarioTableTests(unittest.TestCase):
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

    def test_view_smoke_is_a_separate_principal_fixture(self):
        table = load_scenario("view_smoke")
        self.assertEqual(set(table["principals"]), {"commander", "M1"})
        self.assertEqual(table["predeployed"][1]["name"], "R0")

    def test_rescue_baseline_declares_the_kbk_min_extraction_and_timeout(self):
        table = load_scenario("rescue_baseline")
        self.assertEqual(table["version"], "5-provisional")
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


if __name__ == "__main__":
    unittest.main()
