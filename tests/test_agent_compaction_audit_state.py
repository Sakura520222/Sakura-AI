"""Audit records must not masquerade as new user strategy guidance on resume."""

from backend.services.agent_team.strategy_self_check import StrategySelfCheckState


def test_compaction_audit_does_not_reset_repeated_work_detection():
    messages = []
    for index in range(10):
        if index == 9:
            messages.append({'role': 'user', 'content': '', 'metadata': {
                'context_compaction': {'version': 1, 'applied': True}
            }})
        messages.extend([
            {'role': 'assistant', 'tool_calls': [{'id': str(index), 'function': {
                'name': 'read_file', 'arguments': '{"file_path":"same.py"}'
            }}]},
            {'role': 'tool', 'tool_call_id': str(index), 'content': '{"content":"same"}'},
        ])
    events = StrategySelfCheckState().update(messages)
    assert len(events) == 1
    assert events[0]['metadata']['strategy_self_check']['kind'] == 'tool'
