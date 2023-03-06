import pytest

from briefcase.exceptions import ParseError
from briefcase.platforms.linux import parse_freedesktop_os_release


def test_parse():
    "Content can be parsed from a freedesktop file."
    content = """
KEY1=value
KEY2="quoted value"
KEY3='another quoted value'

# Commented line
KEY4=42
"""
    assert parse_freedesktop_os_release(content) == {
        "KEY1": "value",
        "KEY2": "quoted value",
        "KEY3": "another quoted value",
        "KEY4": "42",
    }


@pytest.mark.parametrize(
    "content, error",
    [
        ("KEY=value\nnot valid content", r"Line 2: 'not valid content'"),
        (
            "KEY=value\nBAD='unbalanced quote",
            r"Line 2: unterminated string literal \(detected at line 1\) \(<unknown>, line 1\)",
        ),
    ],
)
def test_parse_error(content, error):
    with pytest.raises(
        ParseError,
        match=r"Failed to parse output of FreeDesktop os-release file; " + error,
    ):
        parse_freedesktop_os_release(content)
