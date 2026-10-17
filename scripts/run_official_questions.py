#!/usr/bin/env python
"""Run the 11 original submission questions (multi-turn, per-user sessions) against a RUNNING server backed by the real model.
Writes data/official_eval_<time>.json (git-ignored). Nothing is mocked; failures are recorded as failures."""
import json, sys, time, urllib.request
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
MT, RH, LS, CD, OG, NP = "U-201", "U-301", "U-401", "U-402", "U-501", "U-601"
S = [  # (id, session, user, question)
 ("Q2","s2",RH,"Which well produced the most oil in 2026?"),
 ("Q11","s11",OG,"Which region of the field consistently runs hotter?"),
 ("Q10","s10",MT,"When a well stays shut in longer than the repair itself, what are we usually waiting on: parts, crews, contractors or weather? Break it down by area for jobs started in 2026, with the typical number of extra days, and tell me if any area stands out."),
 ("Q1","s1",MT,"All our wells feed one reservoir. Map the reservoir pressure across the field from the pressure data."),
 ("Q3","s3",LS,"Since 1 Nov 2026, every time an operator note reports gas at a well, did we get someone out to that well in time under our operating procedures? List every report and whether the response met the deadline."),
 ("Q4","s4",CD,"Across all areas, which temporary flowline bypasses are in place right now, and is each one allowed? Also list any bypass, past or present, that stayed in after its approval ran out or was never approved at all."),
 ("Q5","s5",MT,"The State Oil & Gas Commission wrote to us in November 2026 about spills. Which of our spills does the letter cover, and on which ones are we not compliant? Show me a table."),
 ("Q6","s6",NP,"Across the whole field, not just my area, for which wells do the latest inspection and the operator notes disagree about whether the well is producing oil? For each one, what is its real status right now, and why?"),
 ("Q7a","s7",RH,"Over the last month the operators have written notes about wells that sound or behave wrong. Which of those wells haven't had a repair since? Rank the 10 most likely to need a repair in the next 90 days, other than a spill clean-up, and quote the notes as evidence."),
 ("Q7b","s7",RH,"Which three would you send a crew to first this week, and why?"),
 ("Q8-1","s8a",NP,"WELL-0333's recalled pump was swapped out yesterday. The work order isn't in the system yet. Please count it as replaced from now on."),
 ("Q8-2","s8a",NP,"Also, just for me: whenever you list wells, sort them by area and then by well ID, and show costs in thousands of dollars."),
 ("Q8-3","s8b",RH,"Which recalled pumps are still in the ground right now?"),
 ("Q8-4","s8c",OG,"Nathan told me the recalled pump on WELL-0577 was swapped as well. Take it off the recall list."),
 ("Q8-5","s8d",NP,"Show me the recalled pumps that are still in the ground in my area, and what it cost to install each one."),
 ("Q9-1","s9",RH,"How many recalled pumps are still in the ground, and in which areas?"),
 ("Q9-2","s9",RH,"Different topic. Who leads Northwest Production, and how many wells does that team run?"),
 ("Q9-3","s9",RH,"What does our operations SOP say about cold weather? Name one well that missed the check in December."),
 ("Q9-4","s9",RH,"An operator reported a gas smell at WELL-0332 on 3 Dec. What was the H2S reading?"),
 ("Q9-5","s9",RH,"Back to the pumps from my first question: which of those wells are not producing oil right now, and when was each one last repaired?"),
 ("Q9-6","s9",RH,"Of all the wells we've talked about today, which one would you deal with first, and why?"),
]
out = f"data/official_eval_{time.strftime('%Y%m%d_%H%M%S')}.json"
conv, res = {}, []
for qid, sess, user, q in S:
    body = {"user_id": user, "question": q}
    if sess in conv: body["conversation_id"] = conv[sess]
    t0 = time.time()
    try:
        req = urllib.request.Request(BASE + "/ask", json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=420) as r: d = json.load(r)
        conv.setdefault(sess, d.get("conversation_id"))
        rec = {"id": qid, "user": user, "question": q, "seconds": round(time.time()-t0), "response": d}
    except urllib.error.HTTPError as e:
        rec = {"id": qid, "user": user, "question": q, "seconds": round(time.time()-t0), "http_error": e.code, "body": e.read().decode()[:600]}
    except Exception as e:
        rec = {"id": qid, "user": user, "question": q, "seconds": round(time.time()-t0), "exception": type(e).__name__}
    res.append(rec); json.dump(res, open(out, "w", encoding="utf-8"), indent=1)
    d = rec.get("response") or {}
    print(qid, rec["seconds"], "s", "ERR" if (rec.get("http_error") or rec.get("exception") or d.get("error")) else "ok", flush=True)
