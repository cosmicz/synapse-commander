"""Independent live minds: one gateway view, model route and action authority each."""
from __future__ import annotations

import copy
import json
import threading
import time
import uuid

from .commander import ClaudeCliRoute, Decision, sha256_text
from ..protocol import IntentEnvelope, ProtocolError


class LiveClaudeRoute(ClaudeCliRoute):
    def __init__(self, model, *, system_prompt, effort='low', **kwargs):
        super().__init__(model, **kwargs)
        self.system_prompt = system_prompt
        self.effort = effort

    def argv(self, prompt):
        argv = super().argv(prompt)
        argv[argv.index('--system-prompt') + 1] = self.system_prompt
        argv.extend(['--effort', self.effort])
        return argv


def parse_mind_decision(text, observation, principal, principals, used_ids, intent_id):
    """Validate typed model output in this principal's context; never infer an action."""
    try:
        stripped = text.strip()
        if stripped.startswith('```') and stripped.endswith('```'):
            stripped = stripped[3:-3].removeprefix('json').strip()
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            # CLI models sometimes add a sentence before the decision. Accept
            # exactly one complete decision object; never choose among several.
            candidates = []
            decoder = json.JSONDecoder()
            for index, char in enumerate(stripped):
                if char != '{':
                    continue
                try:
                    candidate, _ = decoder.raw_decode(stripped[index:])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict) and candidate.get('action') in ('intent', 'wait', 'done'):
                    candidates.append(candidate)
            if len(candidates) != 1:
                raise ValueError('exactly one complete decision object required')
            value = candidates[0]
        if not isinstance(value, dict) or value.get('action') not in ('intent', 'wait', 'done'):
            raise ValueError('one intent, wait or done decision required')
        if value['action'] != 'intent':
            return Decision(value['action'], reason=str(value.get('reason', ''))[:256])
        envelope = value.get('envelope')
        if not isinstance(envelope, dict):
            raise ValueError('intent envelope required')
        if envelope.get('principal', principal) != principal:
            raise ValueError('foreign principal')
        envelope = {**envelope, 'principal': principal}
        parsed = IntentEnvelope.from_untrusted(envelope, principal=principal, team='0',
                                               principals=frozenset(principals))
        if parsed.intent_id != intent_id or parsed.intent_id in used_ids:
            raise ValueError('use the supplied new intent_id')
        if parsed.based_on_observation_id != observation['observation_id']:
            raise ValueError('cite the current observation_id')
        if not observation['frame'] < parsed.expires_at_frame <= observation['frame'] + 900:
            raise ValueError('expiry must be current frame + 900, at most +900')
        permitted = {p['type']: p for p in observation.get('permitted_intents', [])}
        if parsed.type not in permitted:
            raise ValueError('intent type not permitted in this view')
        if parsed.type == 'TASK' and parsed.payload['verb'] not in permitted['TASK'].get('verbs', []):
            raise ValueError('task verb not permitted in this view')
        return Decision('intent', envelope=envelope)
    except (ValueError, TypeError, KeyError, ProtocolError) as error:
        return Decision('invalid', rejected=str(error)[:240])


class LiveMind:
    """No body means no model call. All decisions use only this mind's delivered view."""
    def __init__(self, principal, client, route, sink, *, principals, on_decision=None, on_order=None,
                 call_budget=40, wall_deadline_s=600, poll_s=.5,
                 min_frames_between_calls=90, task_label='mind_task'):
        if principal not in principals or not 1 <= call_budget <= 200 or not 0 < wall_deadline_s <= 3600:
            raise ValueError('invalid principal or model budget')
        self.principal, self.client, self.route, self.sink = principal, client, route, sink
        self.principals, self.on_decision = frozenset(principals), on_decision
        self.on_order = on_order
        self._human_generation, self._human_order, self._paused = 0, None, False
        self.call_budget, self.wall_deadline_s, self.poll_s = call_budget, wall_deadline_s, poll_s
        self.min_frames_between_calls, self.task_label = min_frames_between_calls, task_label
        self._stop, self._lock = threading.Event(), threading.RLock()
        self._thread = None
        self._began, self._last_frame = time.monotonic(), -100000
        self._used_ids, self._history, self._latest = set(), [], {}
        self._messages = []
        self._last_decision_status = None
        self._nonce = uuid.uuid4().hex[:12]
        self._status = 'Waiting for an embodied local view'
        self.trace = {'principal': principal, 'calls': 0, 'decisions': [], 'live': True}

    def accept_order(self, text):
        if self.on_order is None:
            raise ValueError('Human control is not enabled for this mind')
        if (not isinstance(text, str) or not text.strip() or len(text) > 1000
                or any(ord(c) < 32 and c not in '\n\t' for c in text)):
            raise ValueError('Order must contain 1-1000 characters')
        with self._lock:
            if self._stop.is_set() or self.trace['calls'] >= self.call_budget:
                raise ValueError('This run is no longer accepting orders')
            generation = self._human_generation + 1
            self.on_order(text.strip(), generation)
            self._human_generation, self._human_order = generation, text.strip()
            self._paused, self._last_frame = False, -100000
            self._status = 'Order received; reading the local situation'
            return {'order_id': generation, 'status': self._status}

    def pause(self):
        if self.on_order is None:
            raise ValueError('Human control is not enabled for this mind')
        with self._lock:
            if self._stop.is_set():
                raise ValueError('This run has stopped')
            generation = self._human_generation + 1
            self.on_order('', generation)
            self._human_generation, self._paused = generation, True
            self._status = 'Mind paused. Existing game orders may finish.'
            return {'order_id': generation, 'status': self._status}

    def snapshot(self):
        with self._lock:
            return copy.deepcopy({'principal': self.principal, 'status': self._status,
                                  'observation': self._latest, 'model': self.route.model,
                                  'route': self.route.name, 'calls': self.trace['calls'],
                                  'call_budget': self.call_budget, 'running': not self._stop.is_set()})

    def step(self):
        if self._stop.is_set():
            return
        status, observation = self.client.observation()
        if status != 200 or observation.get('principal') != self.principal:
            return
        with self._lock:
            self._latest = observation
            # Delivered reports are transient views. Retain this mind's own
            # received messages so a delegation survives the next observation.
            message_key = lambda r: (r.get('from'), r.get('text'), r.get('observed_frame'), r.get('delivered_frame'))
            for report in observation.get('reports', []):
                if report.get('kind') == 'message' and message_key(report) not in {message_key(r) for r in self._messages}:
                    self._messages.append({**copy.deepcopy(report), 'source_observation_id': observation['observation_id']})
                    self._messages = self._messages[-16:]
            if (observation.get('objective') or {}).get('status') in ('extracted', 'lost', 'timeout', 'no_target'):
                self._status = 'Mission terminal'
                self._stop.set()
                return
            if self._stop.is_set() or self._paused or not observation.get('body'):
                return
            if self.trace['calls'] >= self.call_budget or time.monotonic() - self._began >= self.wall_deadline_s:
                self._status = 'Model budget reached'
                self._stop.set()
                return
            if observation['frame'] - self._last_frame < self.min_frames_between_calls:
                return
            self.trace['calls'] += 1
            call = self.trace['calls']
            self._last_frame = observation['frame']
            intent_id = f'{self.principal}-{self._nonce}-{call}'
            generation = self._human_generation
            prompt = json.dumps({'principal': self.principal, 'current_observation': observation,
                                 'own_recent_actions': self._history[-8:], 'next_intent_id': intent_id,
                                 'last_decision_status': self._last_decision_status,
                                 'remembered_messages': [{**r, 'age_frames': observation['frame'] - r['observed_frame']}
                                                         for r in self._messages],
                                 'human_order': self._human_order,
                                 'reply_contract': 'One JSON decision. For an intent use next_intent_id, current observation_id and expiry current frame + 900.'},
                                sort_keys=True)
            self._status = 'Thinking from local observations'
        reply = self.route.call(prompt)
        live = reply.error is None and self.route.model in reply.model_ids
        decision = (parse_mind_decision(reply.text, observation, self.principal, self.principals,
                                        self._used_ids, intent_id) if live else
                    Decision('invalid', rejected='model unavailable or reported model mismatch'))
        with self._lock:
            canceled = (self._stop.is_set() or generation != self._human_generation
                        or time.monotonic() - self._began >= self.wall_deadline_s)
            envelope = decision.envelope if decision.action == 'intent' and not canceled else None
            label = None
            if envelope:
                label = self.task_label if envelope['type'] == 'TASK' else ('delegate' if envelope['type'] == 'MESSAGE' else 'create_agent')
            payload = {'principal': self.principal, 'inference_id': f'mind-{self.principal}-{call}',
                       'live': live, 'model_id': self.route.model, 'latency_ms': reply.latency_ms,
                       'prompt_sha256': sha256_text(prompt), 'output_sha256': sha256_text(reply.raw),
                       'action': 'discarded' if canceled else decision.action}
            if envelope:
                payload.update(intent_id=envelope['intent_id'], decision=label)
            if generation:
                payload['human_order_id'] = generation
            event = self.sink.record_inference(observation['observation_id'], payload, envelope, label)
            record = {**payload, 'inference_event': event, 'parsed': envelope, 'rejected': decision.rejected}
            self.trace['decisions'].append(record)
            if self.on_decision:
                self.on_decision({**record, 'prompt': prompt, 'raw_output': reply.raw})
            if (self._stop.is_set() or generation != self._human_generation
                    or time.monotonic() - self._began >= self.wall_deadline_s):
                return
            if not event:
                self._status = 'Stopped: inference evidence unavailable'
                self._stop.set()
                return
            if envelope:
                self._used_ids.add(envelope['intent_id'])
                http, receipt = self.client.submit(envelope)
                record.update(http=http, receipt=receipt, intent=envelope)
                self._history.append({'intent': envelope, 'receipt': receipt})
                self._status = 'Action accepted; watching effects' if http == 202 and receipt.get('accepted') else 'Action refused; reassessing'
            else:
                self._status = decision.reason or decision.rejected or 'Watching local world'
                self._last_decision_status = {'action': decision.action, 'reason': self._status}

    def run(self):
        while not self._stop.is_set() and time.monotonic() - self._began < self.wall_deadline_s:
            try:
                self.step()
            except Exception as error:
                self.trace['error_type'] = type(error).__name__
                self._status = 'Stopped after connection or recording error'
                self._stop.set()
            self._stop.wait(self.poll_s)
        self._stop.set()
        self.trace['stop'] = 'stopped_or_budget'
        return self.trace

    def start(self):
        if self._thread is not None:
            raise RuntimeError('mind already started')
        self._thread = threading.Thread(target=self.run, name=f'mind-{self.principal}', daemon=True)
        self._thread.start()
        return self._thread

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=getattr(self.route, 'timeout_s', 60) + getattr(self.client, 'timeout_s', 15) + 5)
        return self.trace
