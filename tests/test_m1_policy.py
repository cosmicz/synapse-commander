import copy
import unittest

from synapse_commander.agents.m1_policy import State, extraction_message, next_action, record_receipt


def view(frame=100, linked=True, reports=None, commands=0):
    return {'principal': 'M1', 'frame': frame,
            'body': {'id': 42, 'source': 'own_body', 'link': linked, 'commands': commands},
            'reports': reports or [],
            'permitted_intents': [{'type': 'MESSAGE'}, {'type': 'TASK', 'verbs': ['move_to']}]}


def message(text='extract at 5392 7040', delivered=90, sender='commander'):
    return {'kind': 'message', 'from': sender, 'text': text,
            'observed_frame': delivered - 5, 'delivered_frame': delivered, 'age_frames': 10}


class TargetPolicyTests(unittest.TestCase):
    def test_status_recurs_every_ten_new_linked_views_and_on_reconnection(self):
        state, first = next_action(view())
        self.assertEqual(first['type'], 'MESSAGE')
        for _ in range(50):
            same, action = next_action(view(), state)
            self.assertEqual(same, state)
            self.assertIsNone(action)
        for frame in range(101, 110):
            state, action = next_action(view(frame), state)
            self.assertIsNone(action)
        state, action = next_action(view(110), state)
        self.assertEqual(action['type'], 'MESSAGE')
        state, action = next_action(view(111, linked=False), state)
        self.assertIsNone(action)
        _, action = next_action(view(112), state)
        self.assertEqual(action['type'], 'MESSAGE')

    def test_only_delivered_commander_messages_are_parsed(self):
        valid = message()
        for patch in ({'from': 'enemy'}, {'kind': 'status'}, {'delivered_frame': 101},
                      {'delivered_frame': None}, {'observed_frame': True},
                      {'observed_frame': 95}, {'text': 'extract at -1 2'},
                      {'text': 'EXTRACT 1.5 2'}, {'text': 'EXTRACT 99999999 2'},
                      {'text': 'EXTRACT 1 2 then attack'}):
            with self.subTest(patch=patch):
                self.assertIsNone(extraction_message(dict(valid, **patch), 100))
        self.assertEqual(extraction_message(message('EXTRACT 1 2'), 100)[1], (1, 2))

    def test_received_instruction_moves_own_body_even_after_link_drops(self):
        observation = view(linked=False, reports=[message()])
        before = copy.deepcopy(observation)
        state, action = next_action(observation)
        self.assertEqual(action['payload'], {'unit': '42', 'verb': 'move_to', 'args': {'x': 5392, 'z': 7040}})
        self.assertEqual(observation, before)
        self.assertTrue(state.busy)
        for frame in range(101, 130):
            state, action = next_action(view(frame, linked=False, reports=[message()]), state)
            self.assertIsNone(action)

    def test_queue_completion_does_not_replay_old_message_or_claim_extraction(self):
        state, _ = next_action(view(reports=[message()]))
        state = record_receipt(state, 202, {'accepted': True}, 'intent-1')
        state, _ = next_action(view(101, commands=1), state)
        state, _ = next_action(view(102, commands=0), state)
        self.assertTrue(state.completed)
        for frame in range(103, 120):
            state, action = next_action(view(frame, reports=[message()]), state)
            self.assertTrue(action is None or action['type'] == 'MESSAGE')
            if action:
                self.assertNotIn('extracted', action['payload']['text'])
        state, action = next_action(view(120, reports=[message('EXTRACT 5000 7000', 115)]), state)
        self.assertEqual(action['payload']['args'], {'x': 5000, 'z': 7000})

    def test_busy_command_defers_new_instruction(self):
        state, _ = next_action(view(reports=[message()]))
        state = record_receipt(state, 202, {'accepted': True}, 'intent-1')
        state, action = next_action(view(105, commands=1, reports=[message('EXTRACT 1 2', 104)]), state)
        self.assertEqual(state.goal, (5392, 7040))
        self.assertTrue(action is None or action['type'] == 'MESSAGE')

    def test_explicit_refusal_retries_at_most_three_times_but_uncertainty_does_not(self):
        for http, accepted in ((409, False), (503, None), (503, False)):
            state, action = next_action(view(linked=False, reports=[message()]))
            count = 1
            state = record_receipt(state, http, {'accepted': accepted}, 'i-1')
            for frame in range(101, 150):
                state, action = next_action(view(frame, linked=False, reports=[message()]), state)
                if action:
                    count += 1
                    state = record_receipt(state, 409, {'accepted': False}, f'i-{count}')
            self.assertEqual(count, 3 if http == 409 else 1)

    def test_wrong_principal_body_terminal_or_permission_cannot_move(self):
        for patch in ({'principal': 'commander'}, {'body': {'id': 9, 'source': 'relay'}},
                      {'objective': {'status': 'extracted'}}, {'permitted_intents': []}):
            observation = view(linked=False, reports=[message()])
            observation.update(patch)
            _, action = next_action(observation)
            self.assertIsNone(action)

    def test_order_done_must_match_the_issued_intent_and_own_body(self):
        state, _ = next_action(view(linked=False, reports=[message()]))
        state = record_receipt(state, 202, {'accepted': True}, 'i-1')
        for unit, intent in ((99, 'i-1'), (42, 'foreign')):
            observation = view(105, linked=False)
            observation['recent_effects'] = [{'effect': 'order_done', 'unit_id': unit, 'intent_id': intent, 'frame': 104}]
            result, _ = next_action(observation, state)
            self.assertTrue(result.busy)
        observation['recent_effects'] = [{'effect': 'order_done', 'unit_id': 42, 'intent_id': 'i-1', 'frame': 104}]
        result, action = next_action(observation, state)
        self.assertFalse(result.busy)
        self.assertTrue(result.completed)
        self.assertIsNone(action)


if __name__ == '__main__':
    unittest.main()
