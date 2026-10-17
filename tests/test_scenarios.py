"""The gadget and gateway read the same bounded public scenario declaration."""
import unittest

from synapse_commander.scenarios import DEFAULT_SCENARIO, SCENARIO_SCHEMA, load_scenario, supported_chassis, supported_controllers


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


if __name__ == "__main__":
    unittest.main()
