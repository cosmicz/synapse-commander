from pathlib import Path
import unittest


GADGET = Path(__file__).parents[1] / "game" / "SynapseCommander.sdd" / "luarules" / "gadgets" / "synapse_bridge.lua"


class GadgetSourceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = GADGET.read_text(encoding="utf-8")

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
            self.source.index("if not use_token(teamID, frame)"),
        )

    def test_synced_reducer_requires_the_public_envelope_schema(self) -> None:
        self.assertIn('intent.schema ~= "IntentEnvelope/v1"', self.source)


if __name__ == "__main__":
    unittest.main()
