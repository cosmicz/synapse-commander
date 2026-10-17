from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest
from synapse_commander.mailbox import MailboxTransport
from synapse_commander.protocol import IntentEnvelope

class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.t=MailboxTransport(Path(self.tmp.name),run_id="r")
    def tearDown(self): self.tmp.cleanup()
    def test_partial_outbox_line_is_not_consumed(self):
        self.t.outbox.write_bytes(b'{"seq":1,"kind":"hello","body":{}')
        self.assertIsNone(self.t.hello()); self.t.outbox.write_bytes(self.t.outbox.read_bytes()+b"}\n")
        self.assertEqual(self.t.hello(),{})
    def test_pending_and_size_cap(self):
        env=IntentEnvelope("IntentEnvelope/v1","i","p","0","o",1,"TASK",{"unit":"1","verb":"advance","args":{}})
        with self.assertRaises(TimeoutError): self.t.submit_intent(env,0)
        self.assertIn(b'"seq":1',self.t.inbox.read_bytes())
        self.assertIn(b'"team":0',self.t.inbox.read_bytes()); self.assertIn(b'"unit_id":1',self.t.inbox.read_bytes())
    def test_events_preserve_sequence(self):
        self.t.outbox.write_text('{"seq":2,"frame":2,"kind":"bye","body":{}}\n{"seq":3,"frame":3,"kind":"hello","body":{}}\n')
        self.assertEqual([x["seq"] for x in self.t.events_since(0)], [2,3])
