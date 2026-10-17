"""A human gives an order; an embodied commander chooses bounded game actions."""
from __future__ import annotations

import copy
import json
import threading
import time

from .commander import ClaudeCliRoute, Decision, parse_decision, sha256_text


SYSTEM_PROMPT = """You are the player's bonded commander in a real-time rescue game.
Carry out the player's latest high-level order using only your current bounded
observation. You occupy the commander body. Other reasoning minds control their
own bodies; communicate using MESSAGE rather than assuming command authority.
The game runs while you think. Never invent observations, unit ids, resources,
permissions or successful outcomes. Accepted orders still need observed effects.
Reply with exactly one JSON object, no prose or reasoning transcript:
 {"action":"intent","envelope":<IntentEnvelope/v1>}
 {"action":"wait","reason":"brief operational status"}
 {"action":"done","reason":"brief operational status"}
Envelope fields: schema, intent_id (unique), based_on_observation_id (this view),
expires_at_frame (current frame + 600), type, payload. Do not supply identity.
Only use permitted_intents from the observation. TASK uses payload
{unit:"owned unit id as string",verb:"move_to",args:{x:integer,z:integer}}
or verb advance with args {}. PRODUCE uses {factory:"name",chassis:"declared id",
controller:"declared chip id",parameters:{}}. MESSAGE uses {to:"principal",text:
"at most 256 characters"}. Coordinates are world coordinates from your view.
Use only declared, funded choices. A relay can extend contact. A lost link is
a game constraint; the mind beyond it can still act on its own local information.
Wait for actual movement or production instead of repeatedly issuing the same
order. Say done only when the requested result is observed, or explain inability.
For extraction messages, the local recipient policy understands 'extract at X Z'.
"""


class BondedClaudeRoute(ClaudeCliRoute):
    def argv(self, prompt):
        argv = super().argv(prompt)
        argv[argv.index('--system-prompt') + 1] = SYSTEM_PROMPT
        return argv


class BondedCommander:
    """One model call at a time; a new order invalidates an in-flight decision.

    on_order records a human-guidance intervention before accepting the order.
    It must keep raw text in private evidence only. sink is the existing ordered
    inference ledger; no action is dispatched without its inference record.
    """
    def __init__(self, client, route, sink, on_order, on_decision=None, *,
                 call_budget=20, wall_deadline_s=400, poll_s=0.5):
        if not 1 <= call_budget <= 100 or not 0 < wall_deadline_s <= 3600:
            raise ValueError('invalid model budget or deadline')
        self.client, self.route, self.sink = client, route, sink
        self.on_order, self.on_decision = on_order, on_decision
        self.call_budget, self.wall_deadline_s, self.poll_s = call_budget, wall_deadline_s, poll_s
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._generation, self._order, self._last_frame = 0, None, -1
        self._latest, self._history, self._used_ids = {}, [], set()
        self._status = 'Waiting for your order'
        self._receipt = None
        self._began = time.monotonic()
        self.trace = {'decisions': [], 'orders': [], 'calls': 0}

    def accept_order(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 1000 or any(ord(c) < 32 and c not in '\n\t' for c in text):
            raise ValueError('Order must contain 1-1000 characters')
        with self._lock:
            if self._stop.is_set() or self.trace['calls'] >= self.call_budget:
                raise ValueError('This run is no longer accepting orders')
            generation = self._generation + 1
            self.on_order(text.strip(), generation)
            self._generation, self._order = generation, text.strip()
            self._history, self._last_frame = [], -1
            self._status = 'Order received. Reading the situation.'
            self.trace['orders'].append({'generation': generation, 'text_sha256': sha256_text(self._order)})
            return {'order_id': generation, 'status': self._status}

    def pause(self):
        with self._lock:
            self.on_order('', self._generation + 1)
            self._generation += 1
            self._order = None
            self._status = 'Mind paused. Existing game orders may still finish.'
            return {'status': self._status}

    def snapshot(self):
        with self._lock:
            return copy.deepcopy({'status': self._status, 'order_id': self._generation,
                                  'order': self._order, 'observation': self._latest,
                                  'receipt': self._receipt, 'calls': self.trace['calls'],
                                  'call_budget': self.call_budget, 'running': not self._stop.is_set()})

    def step(self):
        status, observation = self.client.observation()
        if status != 200 or observation.get('principal') != 'commander':
            return
        with self._lock:
            self._latest = observation
            terminal = (observation.get('objective') or {}).get('status')
            if terminal in ('extracted', 'lost', 'timeout', 'no_target'):
                self._order, self._status = None, f'Mission outcome: {terminal}'
            if self._stop.is_set() or self._order is None or observation['frame'] <= self._last_frame:
                return
            if self.trace['calls'] >= self.call_budget:
                self._order, self._status = None, 'Model call budget reached'
                return
            generation, order = self._generation, self._order
            prompt = json.dumps({'human_order': order, 'current_observation': observation,
                                 'own_recent_actions': self._history[-6:]}, sort_keys=True)
            self.trace['calls'] += 1
            call = self.trace['calls']
            self._last_frame = observation['frame']
            self._status = 'Bonded mind is considering the order'
        reply = self.route.call(prompt)
        decision = parse_decision(reply.text, observation, self._used_ids) if reply.error is None else Decision('invalid', rejected='model unavailable')
        with self._lock:
            canceled = (generation != self._generation or self._stop.is_set()
                        or time.monotonic() - self._began >= self.wall_deadline_s)
            payload = {'principal': 'commander', 'inference_id': f'bonded-{call}',
                       'live': reply.error is None and self.route.model in reply.model_ids,
                       'model_id': self.route.model, 'latency_ms': reply.latency_ms,
                       'prompt_sha256': sha256_text(prompt), 'output_sha256': sha256_text(reply.raw),
                       'action': 'discarded' if canceled else decision.action, 'human_order_id': generation}
            envelope = decision.envelope if decision.action == 'intent' and not canceled else None
            action_label = 'bonded_order' if envelope else None
            if envelope:
                payload.update(intent_id=envelope['intent_id'], decision=action_label)
            event = self.sink.record_inference(observation['observation_id'], payload, envelope, action_label)
            record = {**payload, 'inference_event': event, 'parsed': envelope, 'rejected': decision.rejected}
            if self.on_decision:
                self.on_decision({**record, 'prompt': prompt, 'raw_output': reply.raw})
            self.trace['decisions'].append(record)
            if canceled:
                return
            if not event:
                self._order, self._status = None, 'Paused: inference evidence could not be recorded'
                return
            if envelope:
                self._used_ids.add(envelope['intent_id'])
                http, receipt = self.client.submit(envelope)
                self._receipt = {'http': http, 'accepted': receipt.get('accepted'), 'code': receipt.get('code')}
                record.update(http=http, receipt=receipt, intent=envelope)
                self._history.append({'intent': envelope, 'receipt': receipt})
                self._status = 'Action accepted; watching for its effect' if http == 202 and receipt.get('accepted') else 'Action refused; reassessing'
            elif decision.action == 'done':
                self._order = None
                self._status = (decision.reason or 'Order finished')[:180]
            elif decision.action == 'wait':
                self._status = (decision.reason or 'Watching the situation')[:180]
                self._history.append({'wait': self._status})
            else:
                self._status = 'No valid action from the mind; retrying with the current view'
                self._history.append({'rejected': decision.rejected})

    def run(self):
        while not self._stop.is_set() and time.monotonic() - self._began < self.wall_deadline_s:
            try:
                self.step()
            except Exception as error:
                with self._lock:
                    self._order, self._status = None, 'Mind paused after a connection or recording error'
                    self.trace['error_type'] = type(error).__name__
            self._stop.wait(self.poll_s)
        self._stop.set()
        self.trace['stop'] = 'stopped_or_deadline'
        return self.trace

    def start(self):
        self._thread = threading.Thread(target=self.run, name='bonded-commander', daemon=True)
        self._thread.start()
        return self._thread

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=getattr(self.route, 'timeout_s', 60) + 2)
        return self.trace
