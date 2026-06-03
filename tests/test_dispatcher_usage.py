import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dispatcher import _format_usage_footer


def test_usage_footer_uses_codex_context_window():
    footer = _format_usage_footer(
        {
            "input_tokens": 53_277,
            "_cached_input_tokens": 52_608,
            "output_tokens": 61,
            "_context_window": 258_400,
        },
        "gpt-5.5",
    )

    assert "53.3k / 258.4k" in footer
