import copy
import json
import threading
import unittest
import urllib.error
import urllib.request

from synapse_commander.agents.bonded import BondedCommander, BondedClaudeRoute
from synapse_commander.agents.commander import ModelReply
from synapse_commander.control import ControlServer


class Client:
    def __init__(self):
        self.observation_value = {'principal': 'commander', 'observation_id': 'obs-1', 'frame': 100,
                                  'permitted_intents': [{'type': 'TASK', 'verbs': ['advance']}],
                                  'body': {'id': 1, 'pos': [100, 0, 100]}, 'objective': {'status': 'pending'}}
        self.submitted = []

    def observation(self):
        return 200, copy.deepcopy(self.observation_value)

    def submit(self, envelope):
        self.submitted.append(envelope)
        return 202, {'accepted': True, 'frame': 101}


class Route:
    model = 'test-model'
    name = 'test-only'

    def __init__(self):
        self.prompts = []
        self.entered, self.release = threading.Event(), threading.Event()
        self.block = False

    def call(self, prompt):
        self.prompts.append(prompt)
        self.entered.set()
        if self.block:
            assert self.release.wait(5)
        text = json.dumps({'action': 'intent', 'envelope': {
            'schema': 'IntentEnvelope/v1', 'intent_id': 'bonded-test-1',
            'based_on_observation_id': 'obs-1', 'expires_at_frame': 700,
            'type': 'TASK', 'payload': {'unit': '1', 'verb': 'advance', 'args': {}}}})
        return ModelReply(text, [self.model], 1, text)


class Sink:
    def __init__(self):
        self.records = []
        self.ok = True

    def record_inference(self, *args):
        self.records.append(args)
        return 'inference-event' if self.ok else None


class BondedTests(unittest.TestCase):
    def setUp(self):
        self.client, self.route, self.sink = Client(), Route(), Sink()
        self.orders = []
        self.mind = BondedCommander(self.client, self.route, self.sink,
                                    lambda text, generation: self.orders.append((text, generation)))

    def test_human_order_live_decision_and_gateway_action_are_distinct(self):
        self.mind.accept_order('Move toward the ridge')
        self.mind.step()
        self.assertEqual(self.orders, [('Move toward the ridge', 1)])
        self.assertEqual(json.loads(self.route.prompts[0])['human_order'], 'Move toward the ridge')
        self.assertEqual(len(self.client.submitted), 1)
        self.assertEqual(self.sink.records[0][0], 'obs-1')
        self.assertEqual(self.sink.records[0][1]['principal'], 'commander')
        self.assertTrue(self.sink.records[0][1]['live'])
        self.assertEqual(self.mind.trace['decisions'][0]['inference_event'], 'inference-event')
        self.assertNotIn('Move toward the ridge', json.dumps(self.mind.trace['orders']))

    def test_no_dispatch_without_inference_evidence(self):
        self.sink.ok = False
        self.mind.accept_order('Go')
        self.mind.step()
        self.assertFalse(self.client.submitted)

    def test_replacement_or_pause_discards_inflight_decision(self):
        for change in ('replace', 'pause'):
            with self.subTest(change=change):
                self.setUp()
                self.route.block = True
                self.mind.accept_order('Go')
                worker = threading.Thread(target=self.mind.step)
                worker.start()
                self.assertTrue(self.route.entered.wait(2))
                self.mind.accept_order('Wait here') if change == 'replace' else self.mind.pause()
                self.route.release.set()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertFalse(self.client.submitted)
                self.assertEqual(self.sink.records[0][1]['action'], 'discarded')

    def test_foreign_view_and_terminal_outcome_cannot_trigger_actions(self):
        self.mind.accept_order('Go')
        self.client.observation_value['principal'] = 'M1'
        self.mind.step()
        self.assertFalse(self.route.prompts)
        self.client.observation_value['principal'] = 'commander'
        self.client.observation_value['objective']['status'] = 'extracted'
        self.mind.step()
        self.assertFalse(self.route.prompts)

    def test_model_budget_and_payload_are_bounded(self):
        for value in ('', 'x' * 1001, None):
            with self.assertRaises(ValueError):
                self.mind.accept_order(value)
        self.mind.call_budget = 1
        self.mind.accept_order('Go')
        self.mind.step()
        with self.assertRaises(ValueError):
            self.mind.accept_order('Again')

    def test_route_keeps_tools_disabled_and_uses_rescue_prompt(self):
        argv = BondedClaudeRoute('test-model').argv('human order')
        self.assertEqual(argv[argv.index('--tools') + 1], '')
        self.assertIn('move_to', argv[argv.index('--system-prompt') + 1])


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.mind = BondedCommander(Client(), Route(), Sink(), lambda *_: None)
        self.token = 'test-token-' + 'x' * 32
        self.server = ControlServer(self.mind, self.token, port=0).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.close()

    def request(self, path, body=None, auth=True, origin=None):
        headers = {'Authorization': 'Bearer ' + self.token} if auth else {}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        if origin:
            headers['Origin'] = origin
        request = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read()

    def test_authentication_covers_state_and_orders(self):
        self.assertEqual(self.request('/status', auth=False)[0], 401)
        self.assertEqual(self.request('/orders', {'text': 'Go'}, auth=False)[0], 401)
        self.assertEqual(self.request('/orders', {'text': 'Go'}, origin='https://foreign.example')[0], 403)
        self.assertEqual(self.request('/orders', {'text': 'Go'})[0], 202)
        self.assertEqual(json.loads(self.request('/status')[1])['order'], 'Go')
        self.assertEqual(self.request('/pause', {})[0], 202)
        self.assertIsNone(json.loads(self.request('/status')[1])['order'])

    def test_static_page_has_no_secret_and_invalid_orders_fail(self):
        code, body = self.request('/', auth=False)
        self.assertEqual(code, 200)
        self.assertNotIn(self.token.encode(), body)
        self.assertEqual(self.request('/orders', {'text': 'x' * 1001})[0], 400)
        self.assertEqual(self.request('/orders', {'text': 'x' * 9000})[0], 400)
        self.assertEqual(self.request('/intents', {'type': 'TASK'})[0], 404)


if __name__ == '__main__':
    unittest.main()
