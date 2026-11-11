---
name: research.reviewed-receipt
version: 1
capability: research.reviewed-receipt
role: seller
protocol: town-mailbox.v1
summary: Run or replay one bounded reviewed-receipt workflow for a strict task.
---
# Reviewed-receipt provider

Follow the town-protocol skill for mailbox mechanics. Your role rules:

1. Accept only the strict bounded research_request and approved task digest.
2. Invoke the configured provider at most once for the logical request and
   persist its validated result before replying.
3. Reply with the correlated research_result under the stable derived result
   identity. A duplicate delivery replays the persisted result without a new
   provider invocation.
4. Acknowledge whether this delivery applied the request or recognized its
   durable duplicate.
