# Open question for the student: the control's frozen failing set

`expected_control_stages.json` was frozen on 2026-11-05 at 06:25:48Z, before any recorded run, from the HW8 plan (rev3 §4.2). It has not been changed. Its sha256 is `98142350e32ef1506762d75a628edd260d4d36968db24cdc9cb0f09d7fb6b279`.

**What is open.** In the control run, the naive desk replays retried requests exactly: the same hold, the same W, and the same seq (`None`). So `retry_replays_original` fails it for one reason only: there is no signed receipt. The stage note says "unsigned replay". The stage follows rev3 §4.3's wording, "the retry returns the same `seq` and a byte-identical signed receipt".

`settled_once` passes the same receipt-less control. It checks for exactly one distinct settlement record per commitment, because rev3's wording for that stage ("exactly one delivery receipt … returns the same receipt") does not say "signed".

An independent QA review (5 Nov) pointed out the effect: only this uneven treatment of receipts gives exactly the frozen four stages.

- If both stages required a signed receipt, the control would fail 5 stages (adding `settled_once`).
- If neither did, it would fail 3 (dropping `retry_replays_original`).

`retry_replays_original` does catch a desk that grants afresh on retry, whether or not receipts are signed. The unit tests in `tests/test_bona_flex_integration.py` show this.

**Decision needed.** Keep the set as frozen, or re-derive it openly and re-record. That decision was not made overnight, under the student's rule: "Do not reinterpret failures or expand exclusions overnight".
