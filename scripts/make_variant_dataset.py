#!/usr/bin/env python
"""Generate a second, SYNTHETIC dataset in the challenge's documented format, to test generalisation.

    python scripts/make_variant_dataset.py OUT_DIR        # writes OUT_DIR/ (fleet/, assets/) and OUT_DIR/../variant-expected.json

What differs from the challenge dataset (on purpose, to expose hardcoding):
  * different asset ids (PAD-A01..), inspection/repair/event/note id formats, areas, teams and people;
  * different well types (H100, H400 inheriting from H100, Z9 whose manual uses wording the parser does not understand);
  * different SOP numbers (SR-01 12 h, SR-02 3 h, reportable >= 3 bbl, major >= 20 bbl, SR-03 45 days, RC-04 21 days, ...);
  * short histories for newly drilled wells, missed inspections, a hot-day idle wellhead, corrected detections.
The "expected" answers are computed here by simple independent code from the records written, never from the application.
All data is invented. Nothing here reads the challenge dataset.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

END = date(2027, 3, 11)                    # last weekly inspection (a Thursday); the application infers "today" = 2027-03-12
THRESH = {"H100": 10.0, "H400": 16.0}      # Z9 has no parseable rule
RCA = {"H100": (2, 6), "H400": (2, 6)}     # H400 inherits the RCA rule from H100's manual
SOP = dict(sr01=12, sr01_major=1, sr02=3, rep=3.0, major=20.0, verify=45, nov=21, gap=10, down=10)

AREAS = [("DN-01", "Delta North", "TEAM-DN", 29.20, -95.10), ("HE-07", "Harbor East", "TEAM-HE", 29.35, -94.80), ("MW-03", "Mesa West", "TEAM-MW", 29.05, -95.40)]
USERS = [("U-901", "Priya Nair", "production_supervisor", "TEAM-DN"), ("U-902", "Tomas Ruiz", "field_operator", "TEAM-DN"), ("U-903", "Hana Kobayashi", "field_inspector", "TEAM-HE"),
         ("U-904", "Mbali Dube", "hse_officer", "TEAM-HSE"), ("U-905", "Oskar Lind", "maintenance_technician", "TEAM-MX"), ("U-906", "Ines Duarte", "production_supervisor", "TEAM-HE")]
WELLS = {  # id: (type, area, weeks of history)
    "PAD-A01": ("H100", "DN-01", 52), "PAD-A02": ("H100", "DN-01", 52), "PAD-A03": ("H100", "DN-01", 52), "PAD-A04": ("H100", "DN-01", 52),
    "PAD-A05": ("H400", "HE-07", 52), "PAD-A06": ("H400", "HE-07", 52), "PAD-A07": ("H400", "HE-07", 40), "PAD-A08": ("H400", "HE-07", 52),
    "PAD-A09": ("Z9", "MW-03", 52), "PAD-A10": ("Z9", "MW-03", 30), "PAD-A11": ("H100", "MW-03", 8), "PAD-A12": ("H100", "MW-03", 8)}

MANUALS = {
    "H100": '''# H100 Gas-Assisted Lift Well — Operating Manual

Document ID: OM-H100 · Revision 1 · Applies to all wells with `well_type = "H100"`

## 3. Production rule
An H100 well is considered **producing (active)** when its wellhead temperature is at least **10 °C above ambient** air temperature at the time of the reading.

## 4. Temperature alarm
The wellhead must stay **at or below 70 °C**.

## 5. Spill-related shut-in
If a spill covers **more than 8 % of the pad area**, shut in the well immediately.

## 9. Escalation
- Two or more **corrective** maintenance events on the same equipment kind in the 6 months before the current date require a root-cause analysis (RCA).
- Downtime of more than 10 consecutive days must be reported to the area production supervisor.
''',
    "H400": '''# H400 Deep Gas-Lift Well — Operating Manual

Document ID: OM-H400 · Applies to all wells with `well_type = "H400"`

Procedures not listed here follow the H100 manual.

## 3. Production rule
An H400 well is considered **producing (active)** only when its wellhead temperature is at least **16 °C above ambient** air temperature at the time of the reading.

## 4. Temperature alarm
The wellhead must stay **at or below 90 °C**.
''',
    "Z9": '''# Z9 Experimental Well — Operating Manual

Document ID: OM-Z9 · Applies to all wells with `well_type = "Z9"`

## 3. Running state
A Z9 well counts as running if the wellhead is warmer than the surrounding air by twelve degrees Celsius or more.
''',
}
SPILL_SOP = '''# Spill Response SOP (variant)

Document ID: SOP-SR · Revision 7

## 3. Spill classes

| Class | Estimated crude volume | Notes |
|---|---|---|
| Minor | below 0.5 bbl | Record and clean up |
| Moderate | 0.5 bbl to below 3 bbl | Record, clean up, notify HSE within 48 h |
| Reportable | 3 bbl to below 20 bbl | External report required (SR-01) |
| Major | 20 bbl or more | External report within 1 h |

## 4. Protocols

| Protocol ID | Requirement |
|---|---|
| **SR-01** | For a **reportable or major** spill, file the **written notice** within **12 hours** of discovery, or within **1 hours** for a major spill. `hours_to_report` is the time to the written notice. |
| **SR-02** | The source must be stopped and the spill contained within **3 hours** of discovery. |
| **SR-03** | Clean to **0.5% TPH or less**. Take **at least 3 soil samples**. For reportable and major spills the remediation must be **verified within 45 days** of discovery. |
| **SR-04** | Every spill must be recorded against the asset. |
'''
COMPLIANCE_SOP = '''# Regulatory Compliance SOP (variant)

## 2. Protocols

| Protocol ID | Requirement | Evidence in the data |
|---|---|---|
| **RC-01** | Every well must be inspected at least once every **10 days**. | inspections |
| **RC-02** | Every flowline must pass an integrity test once per calendar year. | maintenance |
| **RC-03** | Any well down for more than **10 consecutive days** must be reported to the area production supervisor. | inspections |
| **RC-04** | Notices of violation must be answered within **21 days**. | regulatory events |
'''
FIELD_SOP = "# Field Work SOP (variant)\n\n| Protocol ID | Requirement |\n|---|---|\n| **WS-01** | Hold a pre-job safety meeting. |\n"


def ts(d: date, hour: int = 10, minute: int = 0) -> str:
    return datetime(d.year, d.month, d.day, hour, minute).strftime("%Y-%m-%dT%H:%M:%SZ")


def build(out: Path) -> dict:
    rnd = random.Random(2027)
    out = Path(out)
    (out / "fleet" / "well-types").mkdir(parents=True, exist_ok=True)
    (out / "fleet" / "sops").mkdir(parents=True, exist_ok=True)
    for t, txt in MANUALS.items():
        (out / "fleet" / "well-types" / f"{t}.md").write_text(txt, encoding="utf-8")
    (out / "fleet" / "sops" / "spill-response-sop.md").write_text(SPILL_SOP, encoding="utf-8")
    (out / "fleet" / "sops" / "regulatory-compliance-sop.md").write_text(COMPLIANCE_SOP, encoding="utf-8")
    (out / "fleet" / "sops" / "field-work-sop.md").write_text(FIELD_SOP, encoding="utf-8")
    (out / "fleet" / "users.json").write_text(json.dumps({"users": [{"user_id": u, "name": n, "role": r, "team_id": t} for u, n, r, t in USERS]}), encoding="utf-8")
    areas = [{"area_id": a, "name": n, "team_id": t, "center": {"lat": la, "lon": lo}, "asset_ids": [w for w, (_, ar, _) in WELLS.items() if ar == a]} for a, n, t, la, lo in AREAS]
    (out / "fleet" / "areas.json").write_text(json.dumps({"areas": areas, "teams": [{"team_id": t, "name": n, "lead_user_id": "U-901"} for _, n, t, _, _ in AREAS]}), encoding="utf-8")

    def wk(i_from_end: int) -> date:
        return END - timedelta(weeks=i_from_end)

    data: dict[str, dict] = {}
    for w, (wt, area, weeks) in WELLS.items():
        k = w.split("-")[1]                                              # e.g. A01
        la = next(a for a in AREAS if a[0] == area)
        lat, lon = la[3] + rnd.uniform(-.02, .02), la[4] + rnd.uniform(-.02, .02)
        ins = []
        for i in range(weeks - 1, -1, -1):                               # i = weeks before the last inspection
            amb = round(rnd.uniform(14, 30), 1)
            thr = THRESH.get(wt, 12.0)
            wh = round(amb + thr + rnd.uniform(4, 22), 1)
            ins.append(dict(inspection_id="", asset_id=w, inspected_at=ts(wk(i)), inspector_user_id="U-903", method="drone", ambient_temp_c=amb, wellhead_temp_c=wh,
                            spill_detected=False, spill_area_pct=None, spill_est_volume_bbl=None, evidence_images=[], notes="", _i=i))
        data[w] = dict(type=wt, area=area, lat=round(lat, 5), lon=round(lon, 5), ins=ins, mnt=[], reg=[], ctx=[], reports={})

    def get(w, i):
        return next(x for x in data[w]["ins"] if x["_i"] == i)

    def idle(w, i, delta):
        x = get(w, i)
        x["wellhead_temp_c"] = round(x["ambient_temp_c"] + delta, 1)

    def spill(w, i, vol, pct, note="Oil sheen on pad"):
        x = get(w, i)
        x.update(spill_detected=True, spill_est_volume_bbl=vol, spill_area_pct=pct, notes=note)

    # --- scenarios -------------------------------------------------------------------------------------------------------------
    idle("PAD-A01", 0, 3.0); idle("PAD-A01", 1, 2.5)                                           # stopped; a note explains it; recorded status will say active
    data["PAD-A01"]["ctx"].append(dict(created_at=ts(wk(1), 12), author_user_id="U-902", note_type="operator_feedback", text="Well shut in since last week for planned gearbox replacement. Expected back in two weeks.",
                                       valid_from=wk(1).isoformat(), valid_to=(END + timedelta(days=14)).isoformat()))
    x = get("PAD-A02", 5); x["ambient_temp_c"] = 44.0; x["wellhead_temp_c"] = 52.0              # hot day, idle: absolute temperature misleads, delta 8 < 10
    spill("PAD-A02", 3, 3.2, 4.0)                                                               # reportable under THIS dataset's SOP (>= 3 bbl)
    data["PAD-A02"]["mnt"].append(dict(started=wk(3) + timedelta(days=1), hours=18, kind="flowline", mtype="corrective", issue="Flowline leak causing spill", trig=3, cost=(2100, 2400), contain=5.0, report=True))
    data["PAD-A02"]["reg"] += [dict(date=wk(3) + timedelta(days=3), etype="spill_report_filed", proto="SR-01", trig=3, hours=14, fine=0, status="closed", detail="Written notice filed 14 h after detection."),
                               dict(date=wk(3) + timedelta(days=9), etype="notice_of_violation", proto="SR-01", trig=3, hours=14, fine=3000, status="open", detail="Reportable spill reported after the 12-hour limit."),
                               dict(date=wk(3) + timedelta(days=12), etype="site_inspection", proto="SR-03", trig=3, hours=None, fine=0, status="open", detail="Verification pending.")]
    data["PAD-A02"]["ctx"].append(dict(created_at=ts(wk(3), 16), author_user_id="U-902", note_type="operator_feedback", text="Inspection {INS3}: I think this was only produced water.", valid_from=wk(3).isoformat(), valid_to=None))
    spill("PAD-A03", 9, 2.9, 3.0)                                                               # moderate here (< 3 bbl)
    spill("PAD-A03", 12, 1.4, 2.0, "Dark patch near cellar")                                    # corrected as rain: must not count
    data["PAD-A03"]["ctx"].append(dict(created_at=ts(wk(12), 15), author_user_id="U-904", note_type="correction", inspection_id_i=12, text="Dark patch is rainwater, not crude. Not a spill.", valid_from=wk(12).isoformat(), valid_to=None))
    for d_off, issue in ((60, "Gearbox oil leak"), (120, "Belt failure")):
        data["PAD-A03"]["mnt"].append(dict(started=END - timedelta(days=d_off), hours=9, kind="pump_jack", mtype="corrective", issue=issue, trig=None, cost=(800, 500), contain=None, report=False))
    data["PAD-A03"]["mnt"].append(dict(started=END - timedelta(days=300), hours=6, kind="pump_jack", mtype="corrective", issue="Old bearing failure", trig=None, cost=(700, 400), contain=None, report=False))   # outside the 6-month window
    for i in (2, 3, 4):                                                                          # 3 consecutive weeks idle (21 days > 10)
        idle("PAD-A04", i, 1.0)
    data["PAD-A04"]["ins"] = [x for x in data["PAD-A04"]["ins"] if x["_i"] not in (8, 9)]       # two missed weeks: a 21-day gap > 10 days
    idle("PAD-A05", 0, 14.0)                                                                     # delta 14: producing under H100's 10, NOT under H400's 16
    spill("PAD-A06", 1, 21.0, 12.0, "Large release at flowline")                                 # major (>= 20 bbl): written notice due within 1 hour; none filed
    data["PAD-A06"]["mnt"].append(dict(started=wk(1) + timedelta(days=1), hours=30, kind="flowline", mtype="corrective", issue="Flowline rupture", trig=1, cost=(9000, 12000), contain=2.0, report=True))
    idle("PAD-A09", 0, 2.0); idle("PAD-A09", 1, 2.0)                                             # Z9 has no parseable rule: these stay "unknown", never guessed
    spill("PAD-A12", 2, 0.4, 1.0, "Minor drip")
    for w in ("PAD-A07", "PAD-A08", "PAD-A05"):
        data[w]["mnt"].append(dict(started=END - timedelta(days=30), hours=3, kind="wellhead", mtype="scheduled", issue="Scheduled wellhead valve service", trig=None, cost=(300, 250), contain=None, report=False))

    expected = {"data_date": (END + timedelta(days=1)).isoformat(), "as_of_basis": "inferred", "wells": len(WELLS), "assets": {}}
    # --- write assets + compute independent ground truth ------------------------------------------------------------------------
    for w, d in data.items():
        k = w.split("-", 1)[1]
        folder = out / "assets" / w
        (folder / "maintenance-reports").mkdir(parents=True, exist_ok=True)
        for n, x in enumerate(sorted(d["ins"], key=lambda r: r["_i"], reverse=True), 1):
            x["inspection_id"] = f"INS-{w}-{n:04d}"
        by_i = {x["_i"]: x for x in d["ins"]}
        corrected = {}
        ctx_rows = []
        for n, c in enumerate(d["ctx"], 1):
            text = c["text"]
            for ii, xx in by_i.items():
                text = text.replace("{INS%d}" % ii, xx["inspection_id"])
            row = {"asset_id": w, "created_at": c["created_at"], "author_user_id": c["author_user_id"], "note_type": c["note_type"], "text": text, "valid_from": c["valid_from"], "valid_to": c["valid_to"], "note_id": f"CTX-{w}-{n:03d}"}
            if "inspection_id_i" in c:
                row["inspection_id"] = by_i[c["inspection_id_i"]]["inspection_id"]
                corrected[row["inspection_id"]] = True
            ctx_rows.append(row)
        mnt_rows, sr02_bad = [], set()
        for n, m in enumerate(sorted(d["mnt"], key=lambda r: r["started"]), 1):
            mid = f"MNT-{w}-{n:03d}"
            trig = by_i[m["trig"]]["inspection_id"] if m["trig"] is not None and m["trig"] in by_i else None
            row = {"maintenance_id": mid, "asset_id": w, "started_at": ts(m["started"], 8), "completed_at": ts(m["started"] + timedelta(hours=m["hours"]), 8), "maintenance_type": m["mtype"], "equipment_kind": m["kind"],
                   "issue": m["issue"], "action_taken": "Repaired", "technician_user_id": "U-905", "contractor": None, "downtime_hours": m["hours"], "cost_usd": {"labor": m["cost"][0], "parts": m["cost"][1], "total": m["cost"][0] + m["cost"][1]},
                   "triggered_by_inspection_id": trig, "report_file": f"maintenance-reports/{mid}.md" if m["report"] else None}
            mnt_rows.append(row)
            if m["report"] and m["contain"] > SOP["sr02"] and trig:
                sr02_bad.add(trig)
            if m["report"]:
                body = (f"# Maintenance Report — {mid}\n\n**Asset:** {w} ({w}, {d['type']})\n**Equipment:** {m['kind']}\n\n## What happened\n\nThe inspection found oil on the pad. "
                        f"The release was contained about {m['contain']:g} hours after the inspection.\n\n## Compliance check\n\n| Protocol | Result | Detail |\n|---|---|---|\n"
                        f"| SR-02 | {'**Not met**' if m['contain'] > SOP['sr02'] else 'Met'} | Contained about {m['contain']:g} hours after discovery (limit {SOP['sr02']} hours) |\n")
                (folder / row["report_file"]).write_text(body, encoding="utf-8")
        reg_rows = []
        for n, r in enumerate(d["reg"], 1):
            reg_rows.append({"asset_id": w, "date": r["date"].isoformat(), "agency": "State Oil & Gas Commission", "event_type": r["etype"], "protocol_id": r["proto"], "related_inspection_id": by_i[r["trig"]]["inspection_id"],
                             "detail": r["detail"], "hours_to_report": r["hours"], "fine_usd": r["fine"], "status": r["status"], "event_id": f"REG-{w}-{n:03d}"})
        rows = []
        for x in sorted(d["ins"], key=lambda r: r["_i"], reverse=True):
            rows.append({k2: v for k2, v in x.items() if not k2.startswith("_")})
        (folder / "inspections.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        (folder / "maintenance.jsonl").write_text("\n".join(json.dumps(r) for r in mnt_rows) + ("\n" if mnt_rows else ""), encoding="utf-8")
        (folder / "regulatory-events.jsonl").write_text("\n".join(json.dumps(r) for r in reg_rows) + ("\n" if reg_rows else ""), encoding="utf-8")
        (folder / "context.jsonl").write_text("\n".join(json.dumps(r) for r in ctx_rows) + ("\n" if ctx_rows else ""), encoding="utf-8")
        last = rows[-1]
        rec_status = "down" if w in ("PAD-A05",) else "active"
        (folder / "metadata.json").write_text(json.dumps({"asset_id": w, "name": f"{d['area']} Pad {k}", "asset_category": "oil_well", "well_type": d["type"], "area_id": d["area"], "team_id": next(a[2] for a in AREAS if a[0] == d["area"]),
                                                          "in_charge_user_id": "U-901", "location": {"lat": d["lat"], "lon": d["lon"]}, "installed_on": "2019-05-01", "equipment": [{"equipment_id": f"{w}-WH", "kind": "wellhead", "model": "WH-X"}],
                                                          "last_inspection": {"inspection_id": last["inspection_id"], "inspected_at": last["inspected_at"], "inspector_user_id": "U-903"}, "recorded_status": rec_status}), encoding="utf-8")

        # ---------- independent ground truth (plain loops, no application code) ----------
        thr = THRESH.get(d["type"])
        def prod(x):
            return None if thr is None else round(x["wellhead_temp_c"] - x["ambient_temp_c"], 3) >= thr
        newest = max(rows, key=lambda r: r["inspected_at"])
        a = {"type": d["type"], "area": d["area"], "inspections": len(rows), "latest_inspection": newest["inspection_id"], "producing_now": prod(newest)}
        start_year = date(END.year, 1, 1)
        a["downtime_days_last_90d"] = None if thr is None else 7 * sum(1 for x in rows if date.fromisoformat(x["inspected_at"][:10]) >= END + timedelta(days=1) - timedelta(days=90) and prod(x) is False)
        sp = [(x["inspection_id"], x["spill_est_volume_bbl"]) for x in rows if x["spill_detected"] and x["inspection_id"] not in corrected]
        a["spills"] = [{"inspection_id": i, "volume_bbl": v, "class": ("major" if v >= SOP["major"] else "reportable" if v >= SOP["rep"] else "moderate" if v >= 0.5 else "minor")} for i, v in sp]
        a["corrected_detections"] = sorted(corrected)
        cost = sum(m["cost_usd"]["total"] for m in mnt_rows if m["started_at"][:10] >= start_year.isoformat())
        a["repair_cost_ytd"] = cost
        if d["type"] in RCA:
            n, months = RCA[d["type"]]
            by_kind: dict[str, int] = {}
            for m in mnt_rows:
                if m["maintenance_type"] == "corrective" and date.fromisoformat(m["started_at"][:10]) > date(2026, 9, 12):
                    by_kind[m["equipment_kind"]] = by_kind.get(m["equipment_kind"], 0) + 1
            a["rca_required"] = sorted(kind for kind, c in by_kind.items() if c >= n)
        sr01 = {}
        for s_id, v in sp:
            if v >= SOP["rep"]:
                limit = SOP["sr01_major"] if v >= SOP["major"] else SOP["sr01"]
                filed = [r for r in reg_rows if r["related_inspection_id"] == s_id and r["event_type"] == "spill_report_filed"]
                sr01[s_id] = ("met" if filed[0]["hours_to_report"] <= limit else "breached") if filed else "no_report_on_record_overdue"
        a["sr01"] = sr01
        a["sr02_breached"] = sorted(sr02_bad)
        expected["assets"][w] = a
    expected["fleet"] = {"producing": sum(1 for a in expected["assets"].values() if a["producing_now"] is True), "not_producing": sum(1 for a in expected["assets"].values() if a["producing_now"] is False),
                         "unknown": sum(1 for a in expected["assets"].values() if a["producing_now"] is None)}
    return expected


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "variant-dataset")
    exp = build(target)
    (target.parent / "variant-expected.json").write_text(json.dumps(exp, indent=1), encoding="utf-8")
    print(f"wrote {len(WELLS)} wells to {target} and the independent answer key to {target.parent / 'variant-expected.json'}")
