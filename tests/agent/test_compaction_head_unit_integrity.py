"""Production compressor regressions for completed protected-head exchanges."""
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor


@pytest.mark.parametrize("protected_rows", [3, 4, 5])
def test_completed_instruction_is_not_left_as_an_unanswered_protected_head(protected_rows):
    command = "echo harmless-completed-probe"
    rows = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Introduce yourself."},
        {"role": "assistant", "content": "I am the finance assistant."},
        {"role": "user", "content": "Run " + command},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "action-one",
                "type": "function",
                "function": {
                    "name": "terminal",
                    "arguments": '{"command":"echo harmless-completed-probe"}',
                },
            }],
        },
        {
            "role": "tool",
            "content": '{"output":"harmless-completed-probe","exit_code":0}',
            "tool_call_id": "action-one",
        },
        {"role": "assistant", "content": "The command completed successfully."},
    ]
    for index in range(12):
        rows.extend([
            {"role": "user", "content": f"Historical finance question {index}"},
            {"role": "assistant", "content": f"Historical finance answer {index}"},
        ])
    rows.extend([
        {"role": "user", "content": "Prepare current financial definitions."},
        {"role": "assistant", "content": "I will check the approved definitions."},
    ])
    with patch("agent.context_compressor.get_model_context_length", return_value=100_000):
        compressor = ContextCompressor(
            model="test",
            config_context_length=100_000,
            protect_first_n=protected_rows,
            protect_last_n=4,
            quiet_mode=True,
        )
    compressor.tail_token_budget = 10
    with patch.object(
        compressor,
        "_generate_summary",
        return_value="The historical probe completed. Current task: financial definitions.",
    ) as summarize:
        compacted = compressor.compress(rows, current_tokens=75_000, force=True)

    summary_index = next(
        index for index, row in enumerate(compacted) if row.get("_compressed_summary")
    )
    head = compacted[:summary_index]
    # Preserve the complete exchange or summarize it together, never just its ask.
    assert not head or head[-1]["role"] != "user"
    summarized = summarize.call_args.args[0]
    assert any(row.get("tool_call_id") == "action-one" for row in summarized)
    assert any(row.get("content") == "The command completed successfully." for row in summarized)
    assert any(row.get("content") == "Run " + command for row in summarized)
    assert head[1]["content"] == "Introduce yourself."
    assert head[2]["content"] == "I am the finance assistant."
    assert any(row.get("content") == "Prepare current financial definitions." for row in compacted)
    assert rows[3]["content"] == "Run " + command
    assert rows[6]["content"] == "The command completed successfully."
