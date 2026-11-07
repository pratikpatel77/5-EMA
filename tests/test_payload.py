"""The dashboard payload has to survive json.dumps(allow_nan=False).

A numpy bool sneaking in from pandas took the whole /api/state response down
with a 500 once; these keep that from coming back.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ema5.service import _clean  # noqa: E402


def dumps(value):
    return json.dumps(_clean(value), allow_nan=False)


def test_numpy_bool_becomes_a_real_bool():
    assert _clean(np.bool_(True)) is True
    assert _clean(np.bool_(False)) is False
    assert dumps({"stop_moved": np.bool_(True)}) == '{"stop_moved": true}'


def test_numpy_numbers_become_plain_numbers():
    assert _clean(np.float64(1.5)) == 1.5
    assert isinstance(_clean(np.float64(1.5)), float)
    assert _clean(np.int64(7)) == 7
    assert dumps({"r": np.float64(2.25), "bars": np.int64(3)}) == '{"r": 2.25, "bars": 3}'


def test_not_a_number_travels_as_null():
    assert _clean(float("nan")) is None
    assert _clean(np.float64("nan")) is None
    assert _clean(float("inf")) is None
    assert dumps({"r_now": math.nan}) == '{"r_now": null}'


def test_nesting_is_cleaned_all_the_way_down():
    payload = {
        "positions": [{"peak": np.float64(3.5), "gap": np.bool_(False),
                       "exit": {"r": float("nan"), "steps": (np.int64(2), np.int64(3))}}],
    }
    assert json.loads(dumps(payload)) == {
        "positions": [{"peak": 3.5, "gap": False, "exit": {"r": None, "steps": [2, 3]}}],
    }


def test_ordinary_values_pass_through_untouched():
    payload = {"symbol": "ASTRAZEN", "ok": True, "n": 3, "x": 1.25, "none": None, "list": ["a"]}
    assert json.loads(dumps(payload)) == payload


@pytest.mark.parametrize("value", [np.float32(2.5), np.int32(4), np.bool_(True), np.float64(0.0)])
def test_every_numpy_scalar_is_serialisable(value):
    json.dumps(_clean(value), allow_nan=False)
