"""Telegram and Weixin split Markdown table rows with the shared helper."""

import pytest

from gateway.platforms import helpers, telegram, weixin


@pytest.mark.parametrize("line,cells", [
    ("| a | b |", ["a", "b"]),
    ("a | b", ["a", "b"]),
    ("  |  x |y|  ", ["x", "y"]),
    ("| only |", ["only"]),
    ("|a||c|", ["a", "", "c"]),
    ("| `x|y` |", ["`x", "y`"]),   # no escaping, as before
])
def test_split_table_row(line, cells):
    assert helpers.split_table_row(line) == cells


def test_adapters_use_the_shared_splitter():
    assert telegram._split_markdown_table_row is helpers.split_table_row
    assert weixin._split_table_row is helpers.split_table_row
