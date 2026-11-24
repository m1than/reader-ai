"""The course delete's typed confirmation (SRV-LESSON FR-LESSON-25). [unit]

@trace FR-LESSON-25
"""

from __future__ import annotations

import pytest

from .conftest import need


@pytest.mark.parametrize("title", ["", " ", "　\t ", " " * 40 + "after forty"])
@pytest.mark.parametrize("confirm", ["", " ", "after forty", None])
def test_an_empty_target_matches_nothing(title: str, confirm: str | None) -> None:
    manage = need("reader.lessons.manage")
    assert manage.confirms(confirm, title) is False


def test_a_non_empty_target_still_matches() -> None:
    manage = need("reader.lessons.manage")
    assert manage.confirms("  Course ", "Course") is True
    assert manage.confirms("", "Course") is False
