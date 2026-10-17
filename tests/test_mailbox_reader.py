"""Actual file-tail probes: partial writes must never erase engine evidence."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from synapse_commander.mailbox import (
    MailboxReadError, MailboxTransport, MAX_OUTBOX_LINE, MAX_READ_BATCH,
)


def line(seq=1, frame=0, kind="hello", body=None):
    return (json.dumps({"seq": seq, "frame": frame, "kind": kind,
                        "body": {} if body is None else body}) + "\n").encode()


class MailboxReaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.transport = MailboxTransport(Path(self.directory.name), run_id="stream")

    def append(self, raw):
        with self.transport.outbox.open("ab") as handle:
            handle.write(raw)

    def test_split_receipt_is_delivered_once_after_newline(self):
        payload = line(kind="receipt", body={"intent_id": "i1", "status": "accepted"})
        for part in (payload[:12], payload[12:-1]):
            self.append(part)
            self.assertIsNone(self.transport.receipt("i1"))
            self.assertEqual(self.transport.events_since(0), [])
        self.append(payload[-1:])
        self.assertEqual(self.transport.receipt("i1")["status"], "accepted")
        self.assertEqual(len(self.transport.events_since(0)), 1)
        self.assertEqual(len(self.transport.events_since(0)), 1)

    def test_complete_prefix_is_not_replayed_while_tail_waits(self):
        self.append(line() + line(2, 15, "observation", {"team": 0})[:-1])
        self.assertEqual([r["seq"] for r in self.transport.events_since(0)], [1])
        self.append(b"\n")
        events = self.transport.events_since(0)
        self.assertEqual([r["seq"] for r in events], [1, 2])
        self.assertLessEqual(events[0]["wall_ns"], events[1]["wall_ns"])
        self.assertEqual(self.transport.current_frame(), 15)

    def test_bad_complete_record_is_not_silently_skipped(self):
        for bad in (b"garbage\n", b"[]\n", b'{"seq":NaN}\n',
                    line(kind=[]), line(body=[]), line(seq=True),
                    line(kind="receipt", body={"intent_id": []}),
                    line(body={"text": "\ud800"})):
            with self.subTest(raw=bad):
                self.transport.outbox.write_bytes(bad + line())
                with self.assertRaises(MailboxReadError):
                    self.transport.events_since(0)
                self.assertEqual(self.transport._offset, 0)

    def test_oversized_line_uses_bounded_read_and_preserves_cursor(self):
        self.append(b"x" * (MAX_OUTBOX_LINE + 10000) + b"\n")
        original_open = Path.open
        sizes = []

        class GuardedReader:
            def __init__(self, handle): self.handle = handle
            def __enter__(self): return self
            def __exit__(self, *args): self.handle.close()
            def seek(self, *args): return self.handle.seek(*args)
            def readline(self, size=-1):
                sizes.append(size)
                if not 0 < size <= MAX_OUTBOX_LINE + 1:
                    raise AssertionError("unbounded read")
                return self.handle.readline(size)
            def read(self, *_args): raise AssertionError("unbounded read")

        def guarded_open(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            return GuardedReader(handle)

        with patch.object(Path, "open", guarded_open):
            with self.assertRaisesRegex(MailboxReadError, "outbox_record_too_large"):
                self.transport.hello()
        self.assertEqual(sizes, [MAX_OUTBOX_LINE + 1])
        self.assertEqual(self.transport._offset, 0)

    def test_missing_sequence_and_time_reversal_are_errors(self):
        self.append(line(1, 15))
        self.transport.hello()
        prefix = self.transport.outbox.read_bytes()
        for bad in (line(3, 30), line(1, 15), line(2, 14)):
            self.transport.outbox.write_bytes(prefix + bad)
            with self.assertRaises(MailboxReadError):
                self.transport.events_since(0)
        self.assertEqual(len(self.transport._events), 1)

    def test_stream_truncation_is_detected(self):
        self.append(line())
        self.transport.hello()
        self.transport.outbox.write_bytes(b"")
        with self.assertRaisesRegex(MailboxReadError, "outbox_truncated"):
            self.transport.hello()

    def test_one_poll_has_bounded_work(self):
        self.append(b"".join(line(i, i) for i in range(1, MAX_READ_BATCH + 3)))
        first = self.transport.events_since(0)
        self.assertEqual(len(first), MAX_READ_BATCH)
        rest = self.transport.events_since(first[-1]["seq"])
        self.assertEqual([r["seq"] for r in rest], [MAX_READ_BATCH + 1, MAX_READ_BATCH + 2])

    def test_concurrent_pollers_do_not_duplicate_records(self):
        self.append(b"".join(line(i, i) for i in range(1, 65)))
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.transport.current_frame(), range(32)))
        self.assertEqual([e["seq"] for e in self.transport.events_since(0)], list(range(1, 65)))
