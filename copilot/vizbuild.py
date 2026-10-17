"""Builders that turn verified tool data into validated visual specs (see visuals.py).

Pure functions: no database access, no model involvement. Every builder returns a dict that has
passed `validate_visual`, so a malformed spec can never reach the frontend.
"""
from __future__ import annotations

from typing import Any

from .visuals import make_table, validate_visual


def _ok(spec: dict) -> dict:
    return validate_visual(spec)


def card(title: str, headline: str, status: str, fields: list[dict], badges: list[str], tool: str, refs: list[str], alt: str, notes=None) -> dict:
    return _ok({"type": "card", "title": title, "headline": headline, "status": status, "fields": fields, "badges": badges,
                "alt_text": alt, "source_tool": tool, "source_refs": refs[:40], "notes": notes or []})


def table(title: str, columns: list[tuple], rows: list[dict], tool: str, refs: list[str], total: int | None = None, notes=None, alt: str | None = None) -> dict:
    return _ok(make_table(title, columns, rows, tool, refs, alt=alt, total=total, notes=notes))


def line(title: str, x_label: str, y_label: str, series: list[dict], markers: list[dict], tool: str, refs: list[str], alt: str, notes=None) -> dict:
    return _ok({"type": "line", "title": title, "x_label": x_label, "y_label": y_label, "series": series, "markers": markers,
                "alt_text": alt, "source_tool": tool, "source_refs": refs[:60], "notes": notes or []})


def bar(title: str, categories: list[str], values: list[float | None], value_label: str, unit: str | None, tool: str, refs: list[str],
        series_name: str | None = None, annotations: list[str] | None = None, notes=None) -> dict:
    alt = f"{title}: " + "; ".join(f"{c} = {v:g}" if v is not None else f"{c} = n/a" for c, v in zip(categories, values))
    return _ok({"type": "bar", "title": title, "categories": categories, "series": [{"name": series_name or value_label, "values": values}],
                "unit": unit, "value_label": value_label, "annotations": annotations or [], "alt_text": alt,
                "source_tool": tool, "source_refs": refs[:40], "notes": notes or []})


def map_points(title: str, points: list[dict], value_label: str | None, tool: str, refs: list[str], skipped: list[str] | None = None, notes=None) -> dict:
    alt = f"{title}: {len(points)} locations. " + "; ".join(f"{p['label']} ({p['lat']:.4f}, {p['lon']:.4f})" for p in points[:8])
    return _ok({"type": "map", "title": title, "points": points, "value_label": value_label, "alt_text": alt, "source_tool": tool,
                "source_refs": refs[:60], "skipped_without_coordinates": skipped or [], "notes": notes or []})


def heatmap(title: str, row_labels: list[str], col_labels: list[str], values: list[list[Any]], unit: str | None, totals: list[float] | None,
            tool: str, refs: list[str], notes=None) -> dict:
    alt = f"{title}: " + "; ".join(f"{r}: " + ", ".join(f"{c}={v}" for c, v in zip(col_labels, row)) for r, row in zip(row_labels, values))
    return _ok({"type": "heatmap", "title": title, "row_labels": row_labels, "col_labels": col_labels, "values": values, "unit": unit,
                "row_totals": totals, "alt_text": alt, "source_tool": tool, "source_refs": refs[:20], "notes": notes or []})


def timeline(title: str, events: list[dict], tool: str, refs: list[str], notes=None) -> dict:
    alt = f"{title}: {len(events)} events. " + "; ".join(f"{e['date']} {e['label']}" for e in events[:10])
    return _ok({"type": "timeline", "title": title, "events": events, "alt_text": alt, "source_tool": tool, "source_refs": refs[:60], "notes": notes or []})


def donut(title: str, slices: list[dict], unit: str | None, tool: str, refs: list[str], notes=None) -> dict:
    alt = f"{title}: " + "; ".join(f"{s['label']} = {s['value']:g}" for s in slices)
    return _ok({"type": "donut", "title": title, "slices": slices, "unit": unit, "alt_text": alt, "source_tool": tool,
                "source_refs": refs[:20], "notes": notes or []})
