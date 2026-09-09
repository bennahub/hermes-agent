"""Real conversation and terminal seams reject historical authority replay."""
import json
from types import SimpleNamespace

from openai.types.chat import ChatCompletion

from agent import auxiliary_client
from hermes_state import SessionDB
from run_agent import AIAgent
from tools import owner_task_authority


def test_completed_probe_cannot_authorize_new_turn_but_fresh_identical_instruction_can(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('TERMINAL_ENV', 'local')
    (tmp_path / 'config.yaml').write_text('model:\n  default: gpt-4.1\n  provider: openai\nterminal:\n  env: local\n')
    marker = tmp_path / 'probe.txt'
    command = "printf 'HERMES_PROBE\n' >> " + str(marker)
    original = 'Run this harmless probe once: ' + command
    unrelated = 'Calculate 2+2 and answer with the number only. Do not run any terminal commands.'
    compiled = []

    def auxiliary(*, messages, **kwargs):
        payload = json.loads(messages[-1]['content'])
        if 'invocation' in payload:
            allowed = payload['frozen_policy']['objective'] == original
            value = {'allowed': allowed, 'reason': 'Only the current explicit probe scope permits terminal execution'}
        elif 'original_instruction' in payload:
            compiled.append(payload['original_instruction'])
            value = {'objective': payload['original_instruction'],
                     'permitted': [payload['original_instruction']], 'excluded': []}
        else:
            value = 'Probe regression'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(value)))])

    monkeypatch.setattr(auxiliary_client, 'call_llm', auxiliary)
    db = SessionDB(tmp_path / 'state.db')
    agent = AIAgent(model='gpt-4.1', provider='openai', api_key='synthetic',
                    base_url='http://127.0.0.1:1/v1', session_id='replay-regression', session_db=db,
                    enabled_toolsets=['terminal'], quiet_mode=True, skip_memory=True,
                    skip_context_files=True, skip_background_review=True, max_iterations=3)
    agent._disable_streaming = True
    calls = []

    def run(instruction, history=None):
        count = 0
        def main(api_kwargs, *args, **kwargs):
            nonlocal count
            count += 1
            calls.append(api_kwargs)
            message = {'role': 'assistant', 'content': 'Completed'}
            if count == 1:
                message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': 'provider-reuses-call-id', 'type': 'function', 'function': {
                        'name': 'terminal', 'arguments': json.dumps({'command': command,
                            'workdir': str(tmp_path), 'timeout': 10})}}]}
            return ChatCompletion.model_validate({'id': 'scripted', 'object': 'chat.completion',
                'created': 1, 'model': 'gpt-4.1', 'choices': [{'index': 0, 'message': message,
                'finish_reason': 'tool_calls' if count == 1 else 'stop'}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 10, 'total_tokens': 110}})
        monkeypatch.setattr(agent, '_interruptible_api_call', main)
        agent._pending_owner_task_nonce = owner_task_authority.mint_pending(
            [agent.session_id], profile_home=str(tmp_path), source='cli', instruction=instruction)
        return agent.run_conversation(instruction, conversation_history=history)

    try:
        first = run(original)
        assert marker.read_text().splitlines() == ['HERMES_PROBE']
        history = first['messages']
        second = run(unrelated, history)
        run(original, second['messages'])
        assert 'HERMES_PROBE' in marker.read_text()
        assert len(calls) >= 3
    finally:
        agent.close()
        db.close()
