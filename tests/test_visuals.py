"""Strict validation of visualization specs: valid payloads pass, malformed or unsafe ones are rejected."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from copilot.visuals import VISUAL_TYPES, make_table, validate_visual

ALT = "alt"


def base(**kw):
    return {"title": "t", "alt_text": ALT, **kw}


VALID = {
    "card": base(type="card", headline="h", fields=[{"label": "a", "value": 1.5, "unit": "C", "status": "bad"}], badges=["x"]),
    "table": base(type="table", columns=[{"key": "a", "label": "A"}], rows=[{"a": 1}, {"a": 2, "_ref": "x"}]),
    "line": base(type="line", x_label="d", y_label="C", series=[{"name": "s", "points": [{"x": "2026-01-01", "y": 1.0}, {"x": "2026-01-08", "y": None}]}],
                 markers=[{"x": "2026-01-08", "label": "not producing"}]),
    "bar": base(type="bar", categories=["a", "b"], series=[{"name": "days", "values": [1, 2]}], value_label="days"),
    "map": base(type="map", points=[{"id": "W", "label": "W", "lat": 32.1, "lon": -102.1, "value": 4.5}]),
    "heatmap": base(type="heatmap", row_labels=["r1"], col_labels=["c1", "c2"], values=[[1, None]]),
    "timeline": base(type="timeline", events=[{"date": "2026-01-01", "label": "x", "kind": "repair"}]),
    "donut": base(type="donut", slices=[{"label": "a", "value": 3}]),
}


def test_every_declared_type_has_a_valid_example():
    assert set(VALID) == set(VISUAL_TYPES)


@pytest.mark.parametrize("name", sorted(VALID))
def test_valid_payloads_round_trip(name):
    out = validate_visual(VALID[name])
    assert out["type"] == name and out["alt_text"] == ALT


@pytest.mark.parametrize("payload,why", [
    (base(type="bar", categories=["a", "b"], series=[{"name": "s", "values": [1]}], value_label="v"), "series length"),
    (base(type="heatmap", row_labels=["r1", "r2"], col_labels=["c"], values=[[1]]), "matrix shape"),
    (base(type="heatmap", row_labels=["r"], col_labels=["c1", "c2"], values=[[1]]), "matrix shape"),
    (base(type="map", points=[{"id": "W", "label": "W", "lat": 95, "lon": 0}]), "latitude range"),
    (base(type="map", points=[{"id": "W", "label": "W", "lat": 0, "lon": -181}]), "longitude range"),
    (base(type="table", columns=[{"key": "a", "label": "A"}], rows=[{"b": 1}]), "row key not in columns"),
    (base(type="donut", slices=[{"label": "a", "value": -1}]), "negative slice"),
    (base(type="line", x_label="d", y_label="y", series=[{"name": "s", "points": [{"x": "d", "y": "abc"}]}]), "non-numeric y"),
    (base(type="card", headline="h", fields=[{"label": "a", "value": 1, "status": "catastrophic"}]), "bad status"),
    ({"type": "html", "title": "t", "alt_text": ALT, "html": "<script>alert(1)</script>"}, "unknown type"),
    ({"type": "table", "title": "t", "columns": [], "rows": []}, "missing alt_text"),
    ({"title": "t", "alt_text": ALT}, "missing type"),
])
def test_invalid_payloads_rejected(payload, why):
    with pytest.raises(ValidationError):
        validate_visual(payload)


def test_make_table_builds_alt_text_and_keeps_private_keys():
    spec = make_table("Wells", [("asset_id", "Well", None), ("delta", "Above air", "C")],
                      [{"asset_id": "WELL-0001", "delta": 3.2, "_ref": "inspection:INS-0001-0203", "ignored": 1}], "tool", ["inspection:INS-0001-0203"])
    assert spec["type"] == "table" and spec["total_rows"] == 1
    assert spec["rows"][0] == {"asset_id": "WELL-0001", "delta": 3.2, "_ref": "inspection:INS-0001-0203"}   # unknown keys dropped
    assert "WELL-0001" in spec["alt_text"] and spec["source_tool"] == "tool"
    validate_visual(spec)
