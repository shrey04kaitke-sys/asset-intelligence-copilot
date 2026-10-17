"""Extraction of rules from manuals/SOPs and structured facts from free-text repair reports.

Everything here is pattern-based and keeps provenance (document, section, excerpt) so the
application can show where a threshold or fact came from. If a pattern does not match, nothing is
invented: the rule is simply absent and dependent checks report "rule unavailable".
"""
from __future__ import annotations

import re
from dataclasses import dataclass

NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}


@dataclass
class RuleRow:
    rule_key: str
    scope_kind: str  # well_type | global
    scope_value: str  # e.g. T200 | '*'
    value_num: float | None
    unit: str | None
    source_doc: str
    source_section: str
    excerpt: str


@dataclass
class ReportFact:
    fact_key: str
    value_num: float | None
    value_text: str | None
    protocol_id: str
    excerpt: str
    extractor: str


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s)


def _sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) pairs."""
    out: list[tuple[str, str]] = []
    head, buf = "(preamble)", []
    for line in text.splitlines():
        if re.match(r"^#{1,4}\s", line):
            out.append((head, "\n".join(buf)))
            head, buf = line.lstrip("# ").strip(), []
        else:
            buf.append(line)
    out.append((head, "\n".join(buf)))
    return out


def _excerpt(m: re.Match, text: str, pad: int = 60) -> str:
    s, e = max(0, m.start() - pad), min(len(text), m.end() + pad)
    return _norm(text[s:e]).strip()


def doc_title(text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return m.group(1).strip() if m else fallback


# ----------------------------------------------------------------------------- manuals

_MANUAL_PATTERNS: list[tuple[str, str, str, int]] = [
    # key, regex, unit, flags
    ("production.min_delta_c", r"at least \*{0,2}(\d+(?:\.\d+)?)\s*°C\s*above\s*ambient", "degC", re.I),
    ("temp_alarm_c", r"at or below \*{0,2}(\d+(?:\.\d+)?)\s*°C", "degC", re.I),
    ("spill.shutin_pad_pct", r"more than \*{0,2}(\d+(?:\.\d+)?)\s*%\s*of the pad", "pct", re.I),
    ("rca.min_corrective_events", r"(\w+) or more \*{0,2}corrective\*{0,2}", "count", re.I),
    ("rca.window_months", r"in the\s+(\d+)\s*months", "months", re.I),
    ("downtime.report_after_days", r"downtime of more than (\d+) consecutive days", "days", re.I),
]


def parse_manual(text: str, doc_id: str) -> tuple[list[str], list[RuleRow]]:
    """Return (well types covered, rule rows). Inheritance ('follows the T200 manual') is a rule row."""
    m = re.search(r'well_type\s*=\s*"?([A-Za-z0-9_-]+)"?', text)
    types = [m.group(1)] if m else []
    rules: list[RuleRow] = []
    if not types:
        return types, rules
    wt = types[0]
    for heading, body in _sections(text):
        flat = _norm(body)
        for key, rx, unit, flags in _MANUAL_PATTERNS:
            for mm in re.finditer(rx, flat, flags):
                raw = mm.group(1).lower()
                val = float(NUM_WORDS[raw]) if raw in NUM_WORDS else _tofloat(raw)
                if val is None:
                    continue
                rules.append(RuleRow(key, "well_type", wt, val, unit, doc_id, heading, _excerpt(mm, flat)))
                break
    flat_all = _norm(text)
    for rx in (r"follow the ([A-Za-z0-9][A-Za-z0-9_-]*) manual", r"Same as ([A-Za-z0-9][A-Za-z0-9_-]*?)(?=\s|[,.;)])"):
        mm = re.search(rx, flat_all)
        if mm and mm.group(1) != wt:
            rules.append(RuleRow("inherits_from", "well_type", wt, None, None, doc_id, "(scope)", mm.group(1)))
            break
    # de-duplicate: keep the first occurrence per key
    seen, uniq = set(), []
    for r in rules:
        if r.rule_key in seen:
            continue
        seen.add(r.rule_key)
        uniq.append(r)
    return types, uniq


def _tofloat(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


# ----------------------------------------------------------------------------- SOPs

def parse_sop(text: str, doc_id: str) -> list[RuleRow]:
    rules: list[RuleRow] = []
    rows = [ln for ln in text.splitlines() if ln.strip().startswith("|")]

    def add(key, val, unit, row, section):
        rules.append(RuleRow(key, "global", "*", float(val), unit, doc_id, section, _norm(row)[:300]))

    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        proto = cells[0].replace("*", "").strip()
        body = _norm(" | ".join(cells[1:]))
        if proto == "SR-01":
            m = re.search(r"within \*{0,2}(\d+) hours\*{0,2} of discovery", body)
            if m:
                add("sr01.report_hours", m.group(1), "hours", row, "SR-01")
            m = re.search(r"within \*{0,2}(\d+) hours\*{0,2} for a major", body)
            if m:
                add("sr01.major_report_hours", m.group(1), "hours", row, "SR-01")
        elif proto == "SR-02":
            m = re.search(r"within \*{0,2}(\d+) hours", body)
            if m:
                add("sr02.contain_hours", m.group(1), "hours", row, "SR-02")
        elif proto == "SR-03":
            m = re.search(r"(\d+(?:\.\d+)?)% TPH or less", body)
            if m:
                add("sr03.tph_target_pct", m.group(1), "pct", row, "SR-03")
            m = re.search(r"at least \*{0,2}(\d+) soil samples", body)
            if m:
                add("sr03.min_soil_samples", m.group(1), "count", row, "SR-03")
            m = re.search(r"verified within \*{0,2}(\d+) days", body)
            if m:
                add("sr03.verify_days", m.group(1), "days", row, "SR-03")
        elif proto == "RC-01":
            m = re.search(r"once every \*{0,2}(\d+) days", body)
            if m:
                add("rc01.max_inspection_gap_days", m.group(1), "days", row, "RC-01")
        elif proto == "RC-03":
            m = re.search(r"more than \*{0,2}(\d+) consecutive days", body)
            if m:
                add("rc03.report_after_days", m.group(1), "days", row, "RC-03")
        elif proto == "RC-04":
            m = re.search(r"within \*{0,2}(\d+) days", body)
            if m:
                add("rc04.nov_response_days", m.group(1), "days", row, "RC-04")
        elif proto.lower() in ("minor", "moderate", "reportable", "major"):
            nums = re.findall(r"(\d+(?:\.\d+)?)\s*bbl", body)
            if proto.lower() != "minor" and nums:
                add(f"spill.class.{proto.lower()}_min_bbl", nums[0], "bbl", row, "Spill classes")
            m = re.search(r"notify HSE within (\d+) h", body)
            if m:
                add("spill.moderate_hse_notice_hours", m.group(1), "hours", row, "Spill classes")
    return rules


# ----------------------------------------------------------------------------- reports

def parse_report(text: str) -> list[ReportFact]:
    facts: list[ReportFact] = []
    flat = _norm(text)
    m = re.search(r"contained about \*{0,2}(\d+(?:\.\d+)?)\s*hours?", flat, re.I)
    if m:
        facts.append(ReportFact("containment_hours", float(m.group(1)), None, "", _excerpt(m, flat, 80), "report:what-happened"))
    m = re.search(r"(\d+(?:\.\d+)?)% of the pad(?: area)?[^.]*?estimated \*{0,2}(\d+(?:\.\d+)?) bbl", flat, re.I)
    if m:
        facts.append(ReportFact("stated_spill_bbl", float(m.group(2)), None, "", _excerpt(m, flat, 80), "report:what-happened"))
    m = re.search(r"(\d+) soil samples taken", flat, re.I)
    if m:
        facts.append(ReportFact("soil_samples", float(m.group(1)), None, "", _excerpt(m, flat, 80), "report:cleanup"))
    m = re.search(r"highest result after clean-?up was (\d+(?:\.\d+)?)% TPH", flat, re.I)
    if m:
        facts.append(ReportFact("max_tph_pct", float(m.group(1)), None, "", _excerpt(m, flat, 80), "report:cleanup"))
    in_table = False
    for ln in text.splitlines():
        if re.match(r"^#{1,4}\s*Compliance check", ln, re.I):
            in_table = True
            continue
        if in_table and re.match(r"^#{1,4}\s", ln):
            in_table = False
        if not in_table or not ln.strip().startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0].lower() in ("protocol", "") or set(cells[0]) <= set("-: "):
            continue
        result = cells[1].replace("*", "").strip().lower()
        proto = cells[0]
        facts.append(ReportFact("compliance", None, result, proto, _norm(ln)[:300], "report:compliance-table"))
        if proto == "SR-04":
            mc = re.search(r"\((\w+) class\)", cells[2])
            if mc:
                facts.append(ReportFact("stated_spill_class", None, mc.group(1).lower(), "SR-04", _norm(ln)[:300], "report:compliance-table"))
    return facts
