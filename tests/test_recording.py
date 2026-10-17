"""Recorder, ledger integrity and run-classification tests (stdlib unittest)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from contextlib import redirect_stdout

from synapse_commander import recording as rec
from synapse_commander.recording import LedgerError, Recorder, verify_ledger

MODEL = "nvidia/Nemotron-3-Nano-30B-A3B"
HASH = "a" * 64


def manifest(**overrides):
    value = {
        "schema": rec.MANIFEST_SCHEMA,
        "run_id": "smoke-001",
        "rig_commit": "76077b7",
        "app_commit": "1a85e05",
        "engine": {"name": "recoil", "version": "2026.07.04"},
        "game": {"name": "bar", "version": "f6f07eaf1fbc09eae536444ff3b2755d81e5440b"},
        "map": {"name": "supreme_isthmus", "version": "v2.1"},
        "scenario": {"id": "broken_relay", "version": "1", "seed": 7},
        "controller_images": {"haul_v1": HASH, "deploy_relay_v1": "b" * 64, "haul_detour_v2": "c" * 64},
        "adapter_version": "loopback/1",
        "principals": ["team1:commander"],
        "config_hashes": {"prompt": "d" * 64},
        "model_id": None,
        "external_budgets": {"max_tokens": 0, "max_requests": 0},
        "clock_mode": "continuous",
        "fps": 30,
        "presentation": "headless",
        "required_decisions": [],
    }
    value.update(overrides)
    return value


AGENT_MANIFEST = dict(
    model_id=MODEL,
    external_budgets={"max_tokens": 20000, "max_requests": 40, "deadline_s": 20},
    required_decisions=[
        {"label": "opening", "type": "PRODUCE"},
        {"label": "recovery", "type": "PRODUCE", "after_scenario_event": "collapse_1"},
    ],
)


class FakeClock:
    def __init__(self, start=1_000_000, step=1_000):
        self.now = start
        self.step = step

    def __call__(self):
        self.now += self.step
        return self.now


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "run.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def recorder(self, **manifest_overrides):
        return Recorder.create(self.path, manifest(**manifest_overrides), clock=FakeClock(), fsync=False)

    # A compact Broken Relay-shaped run.  ``origin`` is the strategic intent
    # origin; ``linked`` controls whether agent intents cite live inference.
    def write_run(self, origin="scripted", linked=True, extra=None, end=True, **manifest_overrides):
        r = self.recorder(**manifest_overrides)
        r.run_start(0)
        ids = {}
        ids["obs1"] = r.record(rec.OBSERVATION, sim_frame=0, origin="gateway",
                               payload={"observation_id": "o1", "team": "team1"})
        ids["opening"] = self._intent(r, origin, "i1", "o1", ids["obs1"], 0, "opening", linked)
        ids["rcpt1"] = r.record(rec.INTENT_RECEIPT, sim_frame=1, origin="gateway",
                                payload={"intent_id": "i1", "status": "accepted"}, causes=[ids["opening"]])
        ids["built"] = r.record(rec.EFFECT, sim_frame=390, origin="engine",
                                payload={"type": "unit_created", "subject": "H1"}, causes=[ids["rcpt1"]])
        ids["step"] = r.record(rec.CONTROLLER_STEP, sim_frame=405, origin="controller",
                               payload={"unit": "H1", "program": "haul_v1", "task": "move", "ops": 12})
        ids["loaded"] = r.record(rec.EFFECT, sim_frame=1200, origin="engine",
                                 payload={"type": "cargo_loaded", "subject": "H1"}, causes=[ids["step"]])
        ids["collapse"] = r.record(rec.SCENARIO_EVENT, sim_frame=1200, origin="scenario",
                                   payload={"name": "collapse_1"}, causes=[ids["loaded"]])
        ids["obs2"] = r.record(rec.OBSERVATION, sim_frame=1500, origin="gateway",
                               payload={"observation_id": "o2", "team": "team1"})
        if extra:
            extra(r, ids)
        ids["recovery"] = self._intent(r, origin, "i2", "o2", ids["obs2"], 1500, "recovery", linked)
        ids["rcpt2"] = r.record(rec.INTENT_RECEIPT, sim_frame=1501, origin="gateway",
                                payload={"intent_id": "i2", "status": "accepted"}, causes=[ids["recovery"]])
        ids["radio"] = r.record(rec.RADIO, sim_frame=2400, origin="engine",
                                payload={"status": "delivered", "bytes": 6144}, causes=[ids["rcpt2"]])
        ids["install"] = r.record(rec.EFFECT, sim_frame=2460, origin="engine",
                                  payload={"type": "controller_activated", "subject": "H1",
                                           "program": "haul_detour_v2"}, causes=[ids["radio"]])
        if end:
            r.run_end(4500, {"success": True, "salvage_unloaded": 80})
        else:
            r.close()  # a crash: no trailer is written
        return ids

    def _intent(self, r, origin, intent_id, obs_id, obs_event, frame, decision, linked):
        causes = [obs_event]
        if origin == "agent" and linked:
            causes = [r.record(rec.INFERENCE, sim_frame=frame, origin="agent",
                               payload={"inference_id": f"inf-{intent_id}", "live": True, "model_id": MODEL,
                                        "request_sha256": HASH, "latency_ms": 900},
                               causes=[obs_event])]
        return r.record(rec.INTENT_SUBMITTED, sim_frame=frame, origin=origin,
                        payload={"intent_id": intent_id, "type": "PRODUCE", "principal": "team1:commander",
                                 "team": "team1", "based_on_observation_id": obs_id,
                                 "expires_at_frame": frame + 900, "decision": decision},
                        causes=causes)

    def lines(self):
        return self.path.read_bytes().split(b"\n")[:-1]

    def write_lines(self, lines, newline=True):
        self.path.write_bytes(b"\n".join(lines) + (b"\n" if newline else b""))

    def rewrite(self, mutate):
        """Mutate decoded records and recompute digests and the trailer.

        This models a careful rewrite, so a passing semantic check here cannot
        be relying on the hash chain.
        """
        records = [json.loads(line) for line in self.lines()]
        records = mutate(records)
        renumbered = {r["event_id"]: rec.event_id_for(seq) for seq, r in enumerate(records)}
        prev = rec.GENESIS
        for seq, record in enumerate(records):
            record["causes"] = [renumbered[c] for c in record["causes"] if c in renumbered]
            record["seq"] = seq
            record["event_id"] = rec.event_id_for(seq)
            record["prev"] = prev
            if record["kind"] == rec.RUN_END:
                record["payload"]["record_count"] = seq + 1
                record["payload"]["chain_head"] = prev
            record["digest"] = rec.record_digest(record)
            prev = record["digest"]
        self.write_lines([rec.canonical_json(r).encode() for r in records])


class ManifestAndOrdering(LedgerCase):
    def test_manifest_is_first_and_precedes_single_run_start(self):
        self.write_run()
        result = verify_ledger(self.path)
        self.assertTrue(result.ok, result.errors)
        kinds = [r["kind"] for r in result.records]
        self.assertEqual(kinds[:2], [rec.MANIFEST, rec.RUN_START])
        self.assertEqual(kinds.count(rec.RUN_START), 1)
        self.assertEqual(kinds[-1], rec.RUN_END)
        self.assertEqual(result.manifest["run_id"], "smoke-001")

    def test_manifest_is_durable_before_run_start(self):
        r = self.recorder()
        on_disk = [json.loads(line) for line in self.lines()]
        self.assertEqual([x["kind"] for x in on_disk], [rec.MANIFEST])
        r.run_start(0)
        r.close()

    def test_recorder_refuses_records_before_run_start(self):
        r = self.recorder()
        with self.assertRaises(LedgerError) as ctx:
            r.record(rec.OBSERVATION, sim_frame=0, origin="gateway", payload={"observation_id": "o"})
        self.assertEqual(ctx.exception.code, "record_before_run_start")
        r.close()

    def test_second_run_start_rejected(self):
        r = self.recorder()
        r.run_start(0)
        with self.assertRaises(LedgerError):
            r.run_start(1)
        r.close()

    def test_caller_cannot_inject_recorder_kinds(self):
        r = self.recorder()
        r.run_start(0)
        for kind in (rec.MANIFEST, rec.RUN_START, rec.RUN_END):
            with self.assertRaises(LedgerError) as ctx:
                r.record(kind, sim_frame=1, origin="recorder", payload={"outcome": {}})
            self.assertEqual(ctx.exception.code, "reserved_kind")
        r.close()

    def test_forged_early_run_end_is_not_a_valid_trailer(self):
        self.write_run()
        lines = self.lines()
        # Drop the real trailer and the last two records, then reuse the real
        # trailer line: its seq, prev and record_count no longer match.
        self.write_lines(lines[:-3] + [lines[-1]])
        result = verify_ledger(self.path)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "corrupt")
        self.assertIsNone(result.classification)

    def test_manifest_not_first_rejected_even_with_recomputed_chain(self):
        self.write_run()
        self.rewrite(lambda rs: [rs[1], rs[0]] + rs[2:])
        result = verify_ledger(self.path)
        self.assertFalse(result.ok)
        self.assertIn("manifest_not_first", result.errors[0])

    def test_record_before_run_start_rejected_by_verifier(self):
        self.write_run()
        self.rewrite(lambda rs: [rs[0], rs[2], rs[1]] + rs[3:])
        result = verify_ledger(self.path)
        self.assertFalse(result.ok)
        self.assertIn("record_before_run_start", result.errors[0])

    def test_refuses_to_overwrite_existing_ledger(self):
        self.path.write_text("existing\n")
        with self.assertRaises(FileExistsError):
            Recorder.create(self.path, manifest(), fsync=False)
        self.assertEqual(self.path.read_text(), "existing\n")

    def test_manifest_requires_identities(self):
        for name in ("rig_commit", "app_commit", "engine", "scenario", "controller_images", "model_id",
                     "external_budgets", "required_decisions"):
            value = manifest()
            del value[name]
            with self.subTest(name=name), self.assertRaises(LedgerError):
                rec.validate_manifest(value)
        with self.assertRaises(LedgerError):
            rec.validate_manifest(manifest(controller_images={"haul_v1": "not-a-hash"}))
        with self.assertRaises(LedgerError):
            rec.validate_manifest(manifest(app_commit="HEAD"))
        with self.assertRaises(LedgerError):
            rec.validate_manifest(manifest(fps=True))
        self.assertFalse(self.path.exists())


class RoundTrip(LedgerCase):
    def test_payloads_and_timings_survive_round_trip(self):
        clock = FakeClock(start=5_000_000_000, step=7)
        r = Recorder.create(self.path, manifest(), clock=clock, fsync=False)
        r.run_start(0)
        payload = {"observation_id": "o1", "note": "réseau ✓", "nested": {"xs": [1, 2.5, None, True]}}
        written = []
        for frame in (0, 15, 15, 30, 7200):
            written.append((r.record(rec.OBSERVATION, sim_frame=frame, origin="gateway",
                                     payload=dict(payload, observation_id=f"o{frame}-{len(written)}")), frame))
        r.run_end(7200, {"success": False})
        result = verify_ledger(self.path)
        self.assertTrue(result.ok, result.errors)
        records = {x["event_id"]: x for x in result.records}
        walls = [x["wall_ns"] for x in result.records]
        self.assertEqual(walls, sorted(walls))
        self.assertEqual(walls[0], 7)  # relative to the recorder's monotonic epoch
        for index, (event_id, frame) in enumerate(written):
            record = records[event_id]
            self.assertEqual(record["sim_frame"], frame)
            self.assertEqual(record["sim_time_s"], frame / 30)
            self.assertEqual(record["payload"], dict(payload, observation_id=f"o{frame}-{index}"))
        self.assertEqual(result.records[-1]["sim_time_s"], 240.0)
        self.assertEqual(rec.replay(self.path), result.records)

    def test_backward_simulation_time_rejected_at_write_and_verify(self):
        r = self.recorder()
        r.run_start(10)
        with self.assertRaises(LedgerError) as ctx:
            r.record(rec.OBSERVATION, sim_frame=9, origin="gateway", payload={"observation_id": "o"})
        self.assertEqual(ctx.exception.code, "time_backwards")
        r.run_end(10, {})
        self.write_run_after_reset()

        def backwards(rs):
            rs[6]["sim_frame"] = 2  # controller step now precedes the effect before it
            rs[6]["sim_time_s"] = rec.sim_time(2, 30)
            return rs
        self.rewrite(backwards)
        self.assertIn("time_backwards", verify_ledger(self.path).errors[0])

    def write_run_after_reset(self):
        self.path.unlink()
        self.write_run()

    def test_concurrent_records_stay_ordered_and_chained(self):
        r = Recorder.create(self.path, manifest(), fsync=False)
        r.run_start(0)
        barrier = threading.Barrier(8)

        def worker(n):
            barrier.wait()
            for i in range(50):
                r.record(rec.OBSERVATION, sim_frame=0, origin="gateway", payload={"observation_id": f"o{n}-{i}"})
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        r.run_end(0, {})
        result = verify_ledger(self.path)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.counts[rec.OBSERVATION], 400)
        self.assertEqual([x["seq"] for x in result.records], list(range(403)))
        walls = [x["wall_ns"] for x in result.records]
        self.assertEqual(walls, sorted(walls))
        self.assertEqual(len({x["payload"].get("observation_id") for x in result.records[2:-1]}), 400)
        with self.assertRaises(LedgerError):
            r.record(rec.OBSERVATION, sim_frame=0, origin="gateway", payload={"observation_id": "late"})

    def test_failed_write_leaves_an_incomplete_ledger(self):
        r = self.recorder()
        r.run_start(0)
        real = r._handle

        class FullDisk:
            closed = False

            def write(self, _text):
                raise OSError("no space")

            def close(self):
                self.closed = True
                real.close()
        r._handle = FullDisk()
        with self.assertRaises(OSError):
            r.record(rec.OBSERVATION, sim_frame=0, origin="gateway", payload={"observation_id": "o"})
        with self.assertRaises(LedgerError):
            r.run_end(0, {})
        self.assertEqual(verify_ledger(self.path).status, "incomplete")

    def test_backward_wall_time_rejected(self):
        self.write_run()

        def backwards(rs):
            rs[4]["wall_ns"] = 0
            return rs
        self.rewrite(backwards)
        result = verify_ledger(self.path)
        self.assertFalse(result.ok)
        self.assertIn("time_backwards", result.errors[0])

    def test_non_monotonic_clock_cannot_be_recorded(self):
        ticks = iter([100, 200, 300, 150])
        r = Recorder.create(self.path, manifest(), clock=lambda: next(ticks), fsync=False)
        r.run_start(0)
        with self.assertRaises(LedgerError):
            r.record(rec.OBSERVATION, sim_frame=0, origin="gateway", payload={"observation_id": "o"})
        r.close()

    def test_sim_time_must_match_frame(self):
        self.write_run()

        def skew(rs):
            rs[3]["sim_time_s"] = 99.0
            return rs
        self.rewrite(skew)
        self.assertIn("sim_time_s", verify_ledger(self.path).errors[0])

    def test_cli_reports_and_exits(self):
        self.write_run()
        out = io.StringIO()
        with redirect_stdout(out):
            code = rec.main(["verify", str(self.path)])
        self.assertEqual(code, 0)
        summary = json.loads(out.getvalue())
        self.assertEqual(summary["classification"], "scripted_replay")
        self.assertFalse(summary["autonomous"])
        self.assertNotIn(str(self.dir), out.getvalue())
        self.path.write_bytes(self.path.read_bytes()[:-5])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(rec.main(["verify", str(self.path)]), 1)


class CorruptionAndTruncation(LedgerCase):
    def assert_refused(self, status, fragment=None):
        result = verify_ledger(self.path)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, status, result.errors)
        self.assertIsNone(result.classification)
        self.assertFalse(result.autonomous)
        if fragment:
            self.assertIn(fragment, result.errors[0])
        with self.assertRaises(LedgerError):
            rec.replay(self.path)
        return result

    def test_mid_record_truncation(self):
        self.write_run()
        data = self.path.read_bytes()
        self.path.write_bytes(data[: len(data) - 40])
        self.assert_refused("truncated")

    def test_whole_line_tail_truncation_is_incomplete(self):
        # Every remaining line is valid JSON with an intact chain; only the
        # missing trailer reveals the loss.
        self.write_run()
        self.write_lines(self.lines()[:-1])
        result = self.assert_refused("incomplete", "RUN_END")
        self.assertEqual(len(result.records), len(self.lines()))

    def test_crash_before_run_end_is_incomplete(self):
        self.write_run(end=False)
        self.assert_refused("incomplete")

    def test_flipped_byte_detected(self):
        self.write_run()
        lines = self.lines()
        lines[5] = lines[5].replace(b'"H1"', b'"H2"', 1)
        self.write_lines(lines)
        self.assert_refused("corrupt", "digest_mismatch")

    def test_deleted_middle_record_detected(self):
        self.write_run()
        lines = self.lines()
        del lines[6]
        self.write_lines(lines)
        self.assert_refused("corrupt", "sequence_gap")

    def test_reordered_records_detected(self):
        self.write_run()
        lines = self.lines()
        lines[4], lines[5] = lines[5], lines[4]
        self.write_lines(lines)
        self.assert_refused("corrupt")

    def test_appended_record_after_trailer_detected(self):
        self.write_run()
        lines = self.lines()
        self.write_lines(lines + [lines[3]])
        self.assert_refused("corrupt", "record_after_end")

    def test_garbage_and_non_utf8_lines(self):
        self.write_run()
        lines = self.lines()
        lines[3] = b"\xff\xfe not json"
        self.write_lines(lines)
        self.assert_refused("corrupt", "not a JSON record")

    def test_empty_and_missing_files(self):
        self.path.write_bytes(b"")
        self.assertEqual(verify_ledger(self.path).status, "empty")
        self.assertEqual(verify_ledger(self.dir / "absent.jsonl").status, "invalid")

    def test_trailer_count_mismatch(self):
        self.write_run()
        lines = self.lines()
        trailer = json.loads(lines[-1])
        trailer["payload"]["record_count"] += 1
        trailer["digest"] = rec.record_digest(trailer)
        lines[-1] = rec.canonical_json(trailer).encode()
        self.write_lines(lines)
        self.assert_refused("corrupt", "trailer_mismatch")


class CausalTyping(LedgerCase):
    def started(self):
        r = self.recorder()
        r.run_start(0)
        obs = r.record(rec.OBSERVATION, sim_frame=0, origin="gateway", payload={"observation_id": "o1"})
        sub = r.record(rec.INTENT_SUBMITTED, sim_frame=0, origin="human",
                       payload={"intent_id": "i1", "type": "TASK", "based_on_observation_id": "o1"},
                       causes=[obs])
        return r, obs, sub

    def test_receipt_is_not_an_effect(self):
        r, _obs, sub = self.started()
        r.record(rec.INTENT_RECEIPT, sim_frame=1, origin="gateway",
                 payload={"intent_id": "i1", "status": "accepted"}, causes=[sub])
        r.run_end(30, {"success": False})
        result = verify_ledger(self.path)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.counts.get(rec.INTENT_RECEIPT), 1)
        self.assertNotIn(rec.EFFECT, result.counts)

    def test_effects_and_receipts_are_distinct_and_ordered(self):
        ids = self.write_run()
        records = {r["event_id"]: r for r in verify_ledger(self.path).records}
        seq = {name: records[eid]["seq"] for name, eid in ids.items()}
        self.assertLess(seq["recovery"], seq["rcpt2"])
        self.assertLess(seq["rcpt2"], seq["radio"])
        self.assertLess(seq["radio"], seq["install"])
        self.assertEqual(records[ids["rcpt2"]]["kind"], rec.INTENT_RECEIPT)
        self.assertEqual(records[ids["install"]]["kind"], rec.EFFECT)
        self.assertEqual(records[ids["install"]]["origin"], "engine")

    def test_rejected_receipt_cannot_cause_effect_or_delivery(self):
        r, _obs, sub = self.started()
        rejected = r.record(rec.INTENT_RECEIPT, sim_frame=1, origin="gateway",
                            payload={"intent_id": "i1", "status": "rejected", "reason_code": "verb_not_supported"},
                            causes=[sub])
        for kind, payload in ((rec.EFFECT, {"type": "moved"}), (rec.RADIO, {"status": "queued"})):
            with self.subTest(kind=kind), self.assertRaises(LedgerError) as ctx:
                r.record(kind, sim_frame=2, origin="engine", payload=payload, causes=[rejected])
            self.assertEqual(ctx.exception.code, "incompatible_cause")
        r.close()

    def test_effect_requires_state_changing_cause_from_engine(self):
        r, obs, sub = self.started()
        with self.assertRaises(LedgerError):
            r.record(rec.EFFECT, sim_frame=1, origin="engine", payload={"type": "moved"})
        with self.assertRaises(LedgerError):
            r.record(rec.EFFECT, sim_frame=1, origin="engine", payload={"type": "moved"}, causes=[sub])
        with self.assertRaises(LedgerError):
            r.record(rec.EFFECT, sim_frame=1, origin="gateway", payload={"type": "moved"}, causes=[obs])
        r.close()

    def test_receipt_must_cite_matching_submission(self):
        r, obs, sub = self.started()
        with self.assertRaises(LedgerError):
            r.record(rec.INTENT_RECEIPT, sim_frame=1, origin="gateway",
                     payload={"intent_id": "other", "status": "accepted"}, causes=[sub])
        with self.assertRaises(LedgerError):
            r.record(rec.INTENT_RECEIPT, sim_frame=1, origin="gateway",
                     payload={"intent_id": "i1", "status": "accepted"}, causes=[obs])
        r.close()

    def test_unknown_and_future_causes_rejected(self):
        r, _obs, _sub = self.started()
        with self.assertRaises(LedgerError) as ctx:
            r.record(rec.SCENARIO_EVENT, sim_frame=1, origin="scenario", payload={"name": "x"},
                     causes=[rec.event_id_for(99)])
        self.assertEqual(ctx.exception.code, "unknown_cause")
        r.close()

    def test_origin_is_typed_per_kind(self):
        r, obs, _sub = self.started()
        cases = [
            (rec.INTENT_SUBMITTED, "operator", {"intent_id": "x", "type": "TASK", "based_on_observation_id": "o1"}),
            (rec.OPERATOR_INTERVENTION, "human", {"action": "console"}),
            (rec.INFERENCE, "human", {"inference_id": "f", "live": True}),
            (rec.SCENARIO_EVENT, "agent", {"name": "collapse_1"}),
            ("ENGINE_COMMAND", "agent", {}),
        ]
        for kind, origin, payload in cases:
            with self.subTest(kind=kind), self.assertRaises(LedgerError):
                r.record(kind, sim_frame=1, origin=origin, payload=payload, causes=[obs])
        r.close()

    def test_repeated_intent_id_is_a_separate_attempt_with_its_own_receipt(self):
        r, obs, sub = self.started()
        first = r.record(rec.INTENT_RECEIPT, sim_frame=1, origin="gateway",
                         payload={"intent_id": "i1", "status": "accepted"}, causes=[sub])
        again = r.record(rec.INTENT_SUBMITTED, sim_frame=2, origin="human",
                         payload={"intent_id": "i1", "type": "TASK", "based_on_observation_id": "o1"}, causes=[obs])
        self.assertNotEqual(again, sub)
        with self.assertRaises(LedgerError) as ctx:
            r.record(rec.INTENT_RECEIPT, sim_frame=2, origin="gateway",
                     payload={"intent_id": "i1", "status": "rejected"}, causes=[sub])
        self.assertEqual(ctx.exception.code, "duplicate_receipt")
        with self.assertRaises(LedgerError):  # one receipt cannot answer both attempts
            r.record(rec.INTENT_RECEIPT, sim_frame=2, origin="gateway",
                     payload={"intent_id": "i1", "status": "rejected", "reason_code": "duplicate_intent_id"},
                     causes=[sub, again])
        r.record(rec.INTENT_RECEIPT, sim_frame=2, origin="gateway",
                 payload={"intent_id": "i1", "status": "rejected", "reason_code": "duplicate_intent_id"},
                 causes=[again])
        r.run_end(3, {})
        result = verify_ledger(self.path)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(result.counts[rec.INTENT_SUBMITTED], 2)
        self.assertEqual(result.counts[rec.INTENT_RECEIPT], 2)
        self.assertNotEqual(first, again)
        self.assertFalse(any("more than once" in x for x in result.evidence_limits))

    def test_double_acceptance_is_recorded_and_flagged_not_refused(self):
        r, obs, sub = self.started()
        r.record(rec.INTENT_RECEIPT, sim_frame=1, origin="gateway",
                 payload={"intent_id": "i1", "status": "accepted"}, causes=[sub])
        again = r.record(rec.INTENT_SUBMITTED, sim_frame=2, origin="human",
                         payload={"intent_id": "i1", "type": "TASK", "based_on_observation_id": "o1"}, causes=[obs])
        r.record(rec.INTENT_RECEIPT, sim_frame=2, origin="gateway",
                 payload={"intent_id": "i1", "status": "accepted"}, causes=[again])
        r.run_end(3, {})
        result = verify_ledger(self.path)
        self.assertTrue(result.ok, result.errors)
        self.assertIn("intent_id i1 accepted more than once", result.evidence_limits)

    def test_decision_does_not_borrow_another_attempts_acceptance(self):
        self.write_run(origin="agent", **AGENT_MANIFEST)

        def reattempt(rs):
            # Insert an earlier "recovery" attempt of the same intent_id that has
            # no receipt; the accepted receipt answers the later, unlabelled one.
            out = []
            for record in rs:
                if record["kind"] == rec.INTENT_SUBMITTED and record["payload"]["intent_id"] == "i2":
                    first = json.loads(json.dumps(record))
                    first["event_id"] = "first-attempt"
                    record["payload"]["decision"] = "retry"
                    out.append(first)
                out.append(record)
            return out
        self.rewrite(reattempt)
        result = verify_ledger(self.path)
        self.assertTrue(result.ok, result.errors)
        attempts = [r for r in result.records if r["kind"] == rec.INTENT_SUBMITTED and r["payload"]["intent_id"] == "i2"]
        receipt = next(r for r in result.records if r["kind"] == rec.INTENT_RECEIPT and r["payload"]["intent_id"] == "i2")
        self.assertEqual([a["payload"]["decision"] for a in attempts], ["recovery", "retry"])
        self.assertEqual(receipt["causes"], [attempts[1]["event_id"]])
        self.assertEqual(result.classification, "agent_partial")


class Classification(LedgerCase):
    def classify(self):
        result = verify_ledger(self.path)
        self.assertTrue(result.ok, result.errors)
        return result

    def test_first_smoke_is_scripted_not_autonomous(self):
        self.write_run(origin="scripted")
        result = self.classify()
        self.assertEqual(result.classification, "scripted_replay")
        self.assertFalse(result.autonomous)
        self.assertIn("scripted: not autonomous gameplay", result.evidence_limits)
        self.assertIn("headless capture: not a visual demo", result.evidence_limits)

    def test_no_strategic_intents_is_scripted_fixture(self):
        r = self.recorder()
        r.run_start(0)
        r.record(rec.SCENARIO_EVENT, sim_frame=10, origin="scenario", payload={"name": "collapse_1"})
        r.run_end(20, {})
        self.assertEqual(self.classify().classification, "scripted_fixture")

    def test_linked_live_agent_run_is_agent_only(self):
        self.write_run(origin="agent", **AGENT_MANIFEST)
        result = self.classify()
        self.assertEqual(result.classification, "agent_only")
        self.assertTrue(result.autonomous)

    def test_operator_intervention_precludes_autonomous(self):
        def intervene(r, ids):
            r.record(rec.OPERATOR_INTERVENTION, sim_frame=1500, origin="operator",
                     payload={"action": "prompt_edit"})
        self.write_run(origin="agent", extra=intervene, **AGENT_MANIFEST)
        result = self.classify()
        self.assertEqual(result.classification, "assisted")
        self.assertFalse(result.autonomous)
        self.assertEqual(result.outcome["success"], True)  # world outcome kept, label is not upgraded

    def test_undeclared_pause_is_intervention_but_spectator_camera_is_not(self):
        def camera(r, ids):
            r.record(rec.PRESENTATION, sim_frame=1500, origin="spectator", payload={"action": "camera_move"})
        self.write_run(origin="agent", extra=camera, **AGENT_MANIFEST)
        self.assertEqual(self.classify().classification, "agent_only")
        self.path.unlink()

        def pause(r, ids):
            r.record(rec.CLOCK_MODE, sim_frame=1500, origin="operator",
                     payload={"mode": "paused", "declared": False})
        self.write_run(origin="agent", extra=pause, **AGENT_MANIFEST)
        self.assertEqual(self.classify().classification, "assisted")

    def test_deleting_an_intervention_breaks_the_chain(self):
        def intervene(r, ids):
            r.record(rec.OPERATOR_INTERVENTION, sim_frame=1500, origin="operator", payload={"action": "console"})
        self.write_run(origin="agent", extra=intervene, **AGENT_MANIFEST)
        self.write_lines([line for line in self.lines() if b"OPERATOR_INTERVENTION" not in line])
        self.assertEqual(verify_ledger(self.path).status, "corrupt")

    def test_forgery_with_recomputed_digests_is_a_documented_limit(self):
        # Without a signing key a careful rewrite that drops the intervention
        # and recomputes every digest verifies; the report says so.
        def intervene(r, ids):
            r.record(rec.OPERATOR_INTERVENTION, sim_frame=1500, origin="operator", payload={"action": "console"})
        self.write_run(origin="agent", extra=intervene, **AGENT_MANIFEST)
        self.rewrite(lambda rs: [r for r in rs if r["kind"] != rec.OPERATOR_INTERVENTION])
        result = self.classify()
        self.assertEqual(result.classification, "agent_only")
        self.assertIn("hash chain detects accidental corruption, not deliberate forgery", result.evidence_limits)

    def test_scripted_intent_mixed_with_agent_is_scripted_replay(self):
        def preload(r, ids):
            sub = r.record(rec.INTENT_SUBMITTED, sim_frame=1500, origin="scripted",
                           payload={"intent_id": "pre", "type": "TASK", "based_on_observation_id": "o2"},
                           causes=[ids["obs2"]])
            r.record(rec.INTENT_RECEIPT, sim_frame=1500, origin="gateway",
                     payload={"intent_id": "pre", "status": "accepted"}, causes=[sub])
        self.write_run(origin="agent", extra=preload, **AGENT_MANIFEST)
        self.assertEqual(self.classify().classification, "scripted_replay")

    def test_human_and_agent_is_centaur(self):
        def human(r, ids):
            r.record(rec.INTENT_SUBMITTED, sim_frame=1500, origin="human",
                     payload={"intent_id": "h1", "type": "TASK", "based_on_observation_id": "o2"},
                     causes=[ids["obs2"]])
        self.write_run(origin="agent", extra=human, **AGENT_MANIFEST)
        result = self.classify()
        self.assertEqual(result.classification, "centaur")
        self.assertFalse(result.autonomous)

    def test_human_only(self):
        self.write_run(origin="human", **AGENT_MANIFEST)
        self.assertEqual(self.classify().classification, "human_only")

    def test_agent_intents_without_inference_are_unattributed(self):
        self.write_run(origin="agent", linked=False, **AGENT_MANIFEST)
        result = self.classify()
        self.assertEqual(result.classification, "unattributed")
        self.assertFalse(result.autonomous)

    def test_decorative_inference_does_not_upgrade(self):
        def decorate(r, ids):
            r.record(rec.INFERENCE, sim_frame=1500, origin="agent",
                     payload={"inference_id": "decor", "live": True, "model_id": MODEL}, causes=[ids["obs2"]])
        self.write_run(origin="agent", linked=False, extra=decorate, **AGENT_MANIFEST)
        self.assertEqual(self.classify().classification, "unattributed")

    def test_inference_must_be_live_and_from_declared_model(self):
        for change in ({"live": False}, {"model_id": "other/model"}):
            with self.subTest(change=change):
                self.path.unlink(missing_ok=True)
                self.write_run(origin="agent", **AGENT_MANIFEST)

                def tweak(rs, change=change):
                    for record in rs:
                        if record["kind"] == rec.INFERENCE:
                            record["payload"].update(change)
                    return rs
                self.rewrite(tweak)
                self.assertEqual(self.classify().classification, "unattributed")

    def test_inference_must_follow_the_intent_observation(self):
        self.write_run(origin="agent", **AGENT_MANIFEST)

        def mismatch(rs):
            for record in rs:
                if record["kind"] == rec.INTENT_SUBMITTED and record["payload"]["intent_id"] == "i2":
                    record["payload"]["based_on_observation_id"] = "o1"
            return rs
        self.rewrite(mismatch)
        self.assertEqual(self.classify().classification, "unattributed")

    def test_empty_required_decisions_never_agent_only(self):
        self.write_run(origin="agent", model_id=MODEL)
        result = self.classify()
        self.assertEqual(result.classification, "agent_partial")
        self.assertFalse(result.autonomous)

    def test_recovery_decision_must_follow_scenario_event(self):
        self.write_run(origin="agent", **AGENT_MANIFEST)

        def relabel(rs):
            # Move the "recovery" label to the opening intent, before collapse_1.
            for record in rs:
                if record["kind"] == rec.INTENT_SUBMITTED:
                    record["payload"]["decision"] = "recovery" if record["payload"]["intent_id"] == "i1" else "opening"
            return rs
        self.rewrite(relabel)
        self.assertEqual(self.classify().classification, "agent_partial")

    def test_required_decision_needs_its_own_accepted_receipt(self):
        self.write_run(origin="agent", **AGENT_MANIFEST)

        def reject(rs):
            out = []
            for record in rs:
                if record["kind"] == rec.INTENT_RECEIPT and record["payload"]["intent_id"] == "i2":
                    record["payload"]["status"] = "rejected"
                    record["payload"]["reason_code"] = "insufficient_material"
                if record["kind"] in (rec.RADIO,) or (record["kind"] == rec.EFFECT
                                                      and record["payload"]["type"] == "controller_activated"):
                    continue
                out.append(record)
            return out
        self.rewrite(reject)
        self.assertEqual(self.classify().classification, "agent_partial")

    def test_missing_scenario_event_fails_required_decision(self):
        self.write_run(origin="agent", **AGENT_MANIFEST)

        def drop_collapse(rs):
            return [r for r in rs if r["kind"] != rec.SCENARIO_EVENT]
        self.rewrite(drop_collapse)
        self.assertEqual(self.classify().classification, "agent_partial")


class SensitiveContent(LedgerCase):
    def test_credentials_and_private_paths_rejected(self):
        r = self.recorder()
        r.run_start(0)
        bad = [
            ({"observation_id": "o", "api_key": "x"}, "credential_rejected"),
            ({"observation_id": "o", "nested": {"Authorization": "x"}}, "credential_rejected"),
            ({"observation_id": "o", "note": "sk-" + "abcdefghijklmnopqrstuv"}, "credential_rejected"),
            ({"observation_id": "o", "log": "/" + "Users/someone/private/x.log"}, "private_path_rejected"),
            ({"observation_id": "o", "log": "see ~/notes"}, "private_path_rejected"),
            ({"observation_id": "o", "transcript": "..."}, "raw_conversation_rejected"),
        ]
        for payload, code in bad:
            with self.subTest(payload=payload), self.assertRaises(LedgerError) as ctx:
                r.record(rec.OBSERVATION, sim_frame=0, origin="gateway", payload=payload)
            self.assertEqual(ctx.exception.code, code)
        r.close()
        self.assertNotIn(b"sk-", self.path.read_bytes())

    def test_manifest_rejects_credentials_before_creating_file(self):
        with self.assertRaises(LedgerError):
            Recorder.create(self.path, manifest(external_budgets={"api_key": "x"}), fsync=False)
        with self.assertRaises(LedgerError):
            Recorder.create(self.path, manifest(adapter_version="/" + "home/someone/adapter"), fsync=False)
        self.assertFalse(self.path.exists())

    def test_budget_token_counts_are_not_credentials(self):
        rec.validate_manifest(manifest(external_budgets={"max_tokens": 100, "token_budget": 5}))

    def test_ledger_never_contains_its_own_path(self):
        self.write_run()
        self.assertNotIn(str(self.dir).encode(), self.path.read_bytes())

    def test_source_contains_no_credentials_or_private_paths(self):
        root = Path(__file__).resolve().parents[1]
        sources = [root / "synapse_commander" / "recording.py", Path(__file__).resolve()]
        private = re.compile(r"/(Users|home|data)/[A-Za-z0-9_.-]+/")
        secret = re.compile(r"sk-[A-Za-z0-9]{20,}|BEGIN [A-Z ]*PRIVATE KEY-----\n")
        for source in sources:
            text = source.read_text(encoding="utf-8")
            with self.subTest(source=source.name):
                self.assertIsNone(private.search(text))
                self.assertIsNone(secret.search(text))


if __name__ == "__main__":
    unittest.main()
