from __future__ import annotations

import pytest

from services.checkout.handler import handle


def test_handle_rejects_none_payload() -> None:
    with pytest.raises(TypeError):
        handle(None)
