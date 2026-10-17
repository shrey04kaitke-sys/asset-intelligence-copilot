"""Typed visualization specs. Data comes only from backend tools; the model may choose a type.

The frontend renders these with fixed, safe renderers (no HTML/JS from the model).
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

VISUAL_TYPES = ["card", "table", "line", "bar", "map", "heatmap", "timeline", "donut"]


class _Base(BaseModel):
    title: str
    alt_text: str = Field(description="Plain-text equivalent of the visual for screen readers")
    source_tool: str = ""
    source_refs: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class CardField(BaseModel):
    label: str
    value: str | float | int | bool | None
    unit: str | None = None
    status: Literal["ok", "warn", "bad", "info", "unknown"] = "info"
    ref: str | None = None


class CardSpec(_Base):
    type: Literal["card"] = "card"
    headline: str
    status: Literal["ok", "warn", "bad", "info", "unknown"] = "info"
    fields: list[CardField]
    badges: list[str] = Field(default_factory=list)


class Column(BaseModel):
    key: str
    label: str
    unit: str | None = None
    align: Literal["left", "right"] = "left"


class TableSpec(_Base):
    type: Literal["table"] = "table"
    columns: list[Column]
    rows: list[dict]
    total_rows: int | None = None

    @model_validator(mode="after")
    def _keys(self):
        keys = {c.key for c in self.columns}
        for r in self.rows:
            if not keys.issuperset(k for k in r if not k.startswith("_")):
                extra = set(k for k in r if not k.startswith("_")) - keys
                raise ValueError(f"row has keys not in columns: {sorted(extra)}")
        return self


class Point(BaseModel):
    x: str
    y: float | None
    ref: str | None = None


class LineSeries(BaseModel):
    name: str
    unit: str | None = None
    points: list[Point]
    style: Literal["solid", "dashed"] = "solid"


class Marker(BaseModel):
    x: str
    label: str
    ref: str | None = None


class LineSpec(_Base):
    type: Literal["line"] = "line"
    x_label: str
    y_label: str
    series: list[LineSeries]
    markers: list[Marker] = Field(default_factory=list)


class BarSeries(BaseModel):
    name: str
    values: list[float | None]


class BarSpec(_Base):
    type: Literal["bar"] = "bar"
    categories: list[str]
    series: list[BarSeries]
    unit: str | None = None
    value_label: str
    annotations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _len(self):
        for s in self.series:
            if len(s.values) != len(self.categories):
                raise ValueError("series length must match categories")
        return self


class MapPoint(BaseModel):
    id: str
    label: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    value: float | None = None
    detail: str = ""
    ref: str | None = None
    tone: Literal["ok", "warn", "bad", "info"] = "info"


class MapSpec(_Base):
    type: Literal["map"] = "map"
    points: list[MapPoint]
    value_label: str | None = None
    skipped_without_coordinates: list[str] = Field(default_factory=list)


class HeatmapSpec(_Base):
    type: Literal["heatmap"] = "heatmap"
    row_labels: list[str]
    col_labels: list[str]
    values: list[list[float | None]]
    unit: str | None = None
    row_totals: list[float] | None = None

    @model_validator(mode="after")
    def _shape(self):
        if len(self.values) != len(self.row_labels) or any(len(r) != len(self.col_labels) for r in self.values):
            raise ValueError("heatmap matrix shape does not match labels")
        return self


class TimelineEvent(BaseModel):
    date: str
    label: str
    kind: Literal["inspection", "repair", "spill", "regulatory", "note", "claim", "other"] = "other"
    detail: str = ""
    ref: str | None = None


class TimelineSpec(_Base):
    type: Literal["timeline"] = "timeline"
    events: list[TimelineEvent]


class Slice(BaseModel):
    label: str
    value: float = Field(ge=0)


class DonutSpec(_Base):
    type: Literal["donut"] = "donut"
    slices: list[Slice]
    unit: str | None = None


VisualSpec = Annotated[
    Union[CardSpec, TableSpec, LineSpec, BarSpec, MapSpec, HeatmapSpec, TimelineSpec, DonutSpec],
    Field(discriminator="type"),
]


class VisualEnvelope(BaseModel):
    spec: VisualSpec


def validate_visual(d: dict) -> dict:
    """Validate a spec dict; returns the normalised dict or raises pydantic.ValidationError."""
    return VisualEnvelope(spec=d).spec.model_dump()


# --- deterministic builders -------------------------------------------------------------

def make_table(title, columns, rows, tool, refs, alt=None, total=None, notes=None) -> dict:
    cols = [Column(key=k, label=l, unit=u) if not isinstance(k, Column) else k for k, l, u in columns]
    keys = [c.key for c in cols]
    clean = [{k: r.get(k) for k in keys} | {kk: vv for kk, vv in r.items() if kk.startswith("_")} for r in rows]
    spec = TableSpec(title=title, columns=cols, rows=clean, total_rows=total if total is not None else len(rows),
                     alt_text=alt or f"{title}: {len(rows)} rows. " + "; ".join(", ".join(str(r.get(k, "")) for k in keys[:4]) for r in rows[:6]),
                     source_tool=tool, source_refs=refs[:40], notes=notes or [])
    return spec.model_dump()


def tone_for(producing: bool | None) -> str:
    return "ok" if producing else ("bad" if producing is False else "info")
