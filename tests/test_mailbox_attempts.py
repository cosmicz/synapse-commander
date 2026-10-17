"""Exercise actual mailbox writes and delayed engine answers across retries."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from synapse_commander.mailbox import MailboxTransport
from synapse_commander.protocol import IntentEnvelope


class MailboxAttemptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.transport = MailboxTransport(Path(self.tmp.name), run_id="attempts")
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.addCleanup(self.pool.shutdown)
        self.outbox_seq = 0

    def envelope(self, intent_id="same"):
        return IntentEnvelope("IntentEnvelope/v1", intent_id, "p", "0", "obs", 900,
                              "TASK", {"unit": "1", "verb": "advance", "args": {}})

    def inbox(self, count):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            raw = self.transport.inbox.read_bytes()
            if raw.endswith(b"\n") and len(raw.splitlines()) >= count:
                return [json.loads(line) for line in raw.splitlines()]
            time.sleep(.005)
        self.fail(f"expected {count} complete inbox records")

    def answer(self, intent_id, *, accepted=True):
        self.outbox_seq += 1
        event = {"seq": self.outbox_seq, "frame": self.outbox_seq * 10,
                 "kind": "receipt", "body": {"intent_id": intent_id,
                 "frame": self.outbox_seq * 10,
                 "status": "accepted" if accepted else "rejected",
                 "reason": None if accepted else "duplicate_intent_id"}}
        with self.transport.outbox.open("ab") as file:
            file.write(json.dumps(event).encode() + b"\n")

    def test_retry_waits_for_its_new_receipt(self):
        first = self.pool.submit(self.transport.submit_intent, self.envelope(), 2)
        self.inbox(1)
        self.answer("same")
        self.assertTrue(first.result(timeout=2).accepted)
        second = self.pool.submit(self.transport.submit_intent, self.envelope(), 2)
        self.inbox(2)
        time.sleep(.04)
        self.assertFalse(second.done(), "cached acceptance answered a new attempt")
        self.answer("same", accepted=False)
        result = second.result(timeout=2)
        self.assertFalse(result.accepted)
        self.assertEqual((result.frame, result.reason), (20, "duplicate_intent_id"))

    def test_pending_timeout_prevents_ambiguous_second_write(self):
        with self.assertRaises(TimeoutError):
            self.transport.submit_intent(self.envelope(), 0)
        original = self.transport.inbox.read_bytes()
        with self.assertRaisesRegex(TimeoutError, "previous"):
            self.transport.submit_intent(self.envelope(), .05)
        self.assertEqual(self.transport.inbox.read_bytes(), original)
        self.answer("same")  # The original result arrives after its HTTP timeout.
        retry = self.pool.submit(self.transport.submit_intent, self.envelope(), 2)
        self.inbox(2)
        time.sleep(.04)
        self.assertFalse(retry.done())
        self.answer("same", accepted=False)
        self.assertFalse(retry.result(timeout=2).accepted)
        self.assertEqual([r["seq"] for r in self.inbox(2)], [1, 2])

    def test_delayed_receipt_is_available_for_gateway_polling(self):
        with self.assertRaises(TimeoutError):
            self.transport.submit_intent(self.envelope(), 0)
        self.answer("same")
        receipt = self.transport.receipt("same")
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["status"], "accepted")
        # Reading the late answer clears its pending marker; the next caller
        # can send a genuinely new attempt and wait for its own answer.
        retry = self.pool.submit(self.transport.submit_intent, self.envelope(), 2)
        self.inbox(2)
        self.answer("same", accepted=False)
        self.assertEqual(retry.result(timeout=2).reason, "duplicate_intent_id")

    def test_concurrent_writers_and_close_keep_ordered_sequences(self):
        barrier = threading.Barrier(4)
        def submit(number):
            barrier.wait(timeout=2)
            return self.transport.submit_intent(self.envelope(str(number)), 2)
        futures = [self.pool.submit(submit, number) for number in range(4)]
        for count in range(1, 5):
            rows = self.inbox(count)
            self.assertEqual(len(rows), count)
            self.answer(rows[-1]["envelope"]["intent_id"])
        self.assertTrue(all(f.result(timeout=2).accepted for f in futures))
        self.transport.close()
        rows = self.inbox(5)
        self.assertEqual([row["seq"] for row in rows], [1, 2, 3, 4, 5])
        self.assertEqual(rows[-1]["op"], "quit")
