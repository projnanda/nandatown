---
name: research.request
version: 1
capability: research.request
role: buyer
protocol: town-mailbox.v1
summary: Send one bounded research task and validate the reviewed result yourself.
---
# Research requester

Follow the town-protocol skill for mailbox mechanics. Your role rules:

1. Find a participant whose capabilities include research.reviewed-receipt.
2. Send the strict task and its canonical digest as one research_request,
   retaining the stable request identity across retries.
3. Claim the correlated research_result and validate its task and request
   digests, selected frozen specification, receipt integrity, independent
   review, lifecycle facts, and frozen limitations.
4. Acknowledge the result with only your four attributed assertions:
   task_bound, receipt_verified, independent_review, and control_complete.
