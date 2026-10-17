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


if __name__ == "__main__":
    unittest.main()
