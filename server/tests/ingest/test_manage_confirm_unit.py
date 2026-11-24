"""The material delete's typed confirmation (SRV-INGEST FR-INGEST-31). [unit]

@trace FR-INGEST-31
"""

from __future__ import annotations

import pytest

from .conftest import need


@pytest.mark.parametrize("title", ["", " ", "　\t ", " " * 40 + "after forty"])
@pytest.mark.parametrize("confirm", ["", " ", "after forty", None])
def test_an_empty_target_matches_nothing(title: str, confirm: str | None) -> None:
    manage = need("reader.ingest.manage")
    assert manage.confirm_matches(confirm, title) is False


def test_a_non_empty_target_still_matches() -> None:
    manage = need("reader.ingest.manage")
    assert manage.confirm_matches("  Book ", "Book") is True
    assert manage.confirm_matches("", "Book") is False
