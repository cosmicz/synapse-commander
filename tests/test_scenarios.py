"""The gadget and gateway read the same bounded public scenario declaration."""
import unittest

from synapse_commander.scenarios import DEFAULT_SCENARIO, SCENARIO_SCHEMA, _validate, load_scenario, supported_chassis, supported_controllers


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


if __name__ == "__main__":
    unittest.main()
