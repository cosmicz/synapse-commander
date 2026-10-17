"""Scripted stranded-mind decisions from its own bounded view; no I/O or truth."""
from dataclasses import dataclass, replace
import re


EXTRACT = re.compile(r'extract(?:\s+at)?\s+([0-9]{1,8})\s+([0-9]{1,8})', re.IGNORECASE)
TERMINAL = frozenset({'extracted', 'lost', 'timeout', 'no_target'})


@dataclass(frozen=True)
class State:
    last_frame: int = -1
    ticks: int = 0
    linked_ticks: int = 0
    status_at: int = -10
    was_linked: bool = False
    message_key: tuple | None = None
    goal: tuple[int, int] | None = None
    attempts: int = 0
    retry_tick: int = 0
    busy: bool = False
    completed: bool = False
    saw_commands: bool = False
    intent_id: str | None = None
    dispatch_frame: int = -1


def extraction_message(report, frame):
    """Return a stable delivery key and integer coordinates, or None.

    Coordinates have a representation bound; map bounds and ground legality
    remain the engine's decision. A delivered message is never a command right
    over another body: this policy may only choose a task for its own body.
    """
    if not isinstance(report, dict) or report.get('kind') != 'message' or report.get('from') != 'commander':
        return None
    observed, delivered = report.get('observed_frame'), report.get('delivered_frame')
    if type(observed) is not int or type(delivered) is not int or not 0 <= observed <= delivered <= frame:
        return None
    text = report.get('text')
    if not isinstance(text, str) or len(text) > 256:
        return None
    match = EXTRACT.fullmatch(text.strip())
    if not match:
        return None
    goal = tuple(int(value) for value in match.groups())
    if any(value >= 2**24 for value in goal):
        return None
    return (delivered, observed, text), goal


def record_receipt(state, http, receipt, intent_id):
    """Call only for the extraction TASK just proposed by next_action.

    A definite rejection may be retried, at most three attempts per message.
    A missing/ambiguous receipt stays pending: transport failure is not proof
    that the engine did nothing. Do not feed status-message receipts here.
    """
    if not state.busy or state.intent_id is not None:
        return state
    if http in (400, 403, 404, 409, 422, 429) and isinstance(receipt, dict) and receipt.get('accepted') is False:
        return replace(state, busy=False, retry_tick=state.ticks + 10,
                       intent_id=intent_id, saw_commands=False)
    return replace(state, intent_id=intent_id)


def next_action(observation, state=None):
    """Return (new State, {label, type, payload} or None) without side effects.

    Count distinct increasing observations, not polling calls. The caller owns
    envelope numbering, gateway submission and receipt/effect recording. A
    queue-empty transition finishes this local command, not the mission.
    """
    state = State() if state is None else state
    frame = observation.get('frame')
    body = observation.get('body') or {}
    if (observation.get('principal') != 'M1' or type(frame) is not int
            or frame <= state.last_frame or body.get('source') != 'own_body'
            or type(body.get('id')) is not int or body['id'] < 0
            or (observation.get('objective') or {}).get('status') in TERMINAL):
        return state, None
    linked = body.get('link') is True
    reconnected = linked and not state.was_linked
    state = replace(state, last_frame=frame, ticks=state.ticks + 1,
                    linked_ticks=state.linked_ticks + 1 if linked else 0,
                    was_linked=linked)
    commands = body.get('commands')
    if state.busy and frame > state.dispatch_frame:
        done = any(isinstance(effect, dict) and effect.get('effect') == 'order_done'
                   and state.intent_id is not None and effect.get('intent_id') == state.intent_id
                   and effect.get('unit_id', effect.get('unit')) == body['id']
                   and type(effect.get('frame')) is int and state.dispatch_frame <= effect['frame'] <= frame
                   for effect in observation.get('recent_effects') or [])
        if done or (state.saw_commands and type(commands) is int and commands == 0):
            state = replace(state, busy=False, completed=True)
        elif type(commands) is int and commands > 0:
            state = replace(state, saw_commands=True)
    candidates = [value for report in observation.get('reports') or []
                  if (value := extraction_message(report, frame)) is not None]
    if not state.busy and candidates:
        key, goal = max(candidates, key=lambda candidate: candidate[0])
        if state.message_key is None or key > state.message_key:
            state = replace(state, message_key=key, goal=goal, attempts=0,
                            completed=False, retry_tick=0, intent_id=None, saw_commands=False)
    permissions = observation.get('permitted_intents') or []
    can_move = any(p.get('type') == 'TASK' and 'move_to' in (p.get('verbs') or []) for p in permissions)
    if (state.goal is not None and not state.busy and not state.completed
            and state.attempts < 3 and state.ticks >= state.retry_tick and can_move
            and type(commands) is int and commands == 0):
        state = replace(state, busy=True, attempts=state.attempts + 1,
                        intent_id=None, dispatch_frame=frame, saw_commands=False)
        return state, {'label': 'to-extraction', 'type': 'TASK',
                       'payload': {'unit': str(body['id']), 'verb': 'move_to',
                                   'args': {'x': state.goal[0], 'z': state.goal[1]}}}
    if linked and (reconnected or state.linked_ticks - state.status_at >= 10):
        if any(p.get('type') == 'MESSAGE' for p in permissions):
            state = replace(state, status_at=state.linked_ticks)
            activity = 'extraction move requested' if state.busy else 'holding position'
            if state.busy and state.saw_commands:
                activity = 'executing extraction move'
            if state.completed:
                activity = 'movement command finished; awaiting mission outcome'
            return state, {'label': 'status', 'type': 'MESSAGE',
                           'payload': {'to': 'commander', 'text': f'M1: alive, mobility degraded, {activity}'}}
    return state, None
