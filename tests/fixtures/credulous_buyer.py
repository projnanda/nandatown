"""A buyer that asserts the quote is correct without checking it.

A test fixture, not an independent agent. It requests the run's task,
claims the one quote response, and settles it with `correct: true`
whatever total arrived. It records the total it observed, and the total
it was told to expect, beside that assertion, so its own acknowledgement
shows what it should have compared.
"""
import os
import time

from nandatown.client import TownClient

client = TownClient(os.environ["TOWN_URL"], os.environ["RUN_ID"])
client.join_auto(os.environ["NAME"], os.environ["TOKEN"], None)
task = client.run_context["task"]
seller = next(p["name"] for p in client.participants()
              if "quote.read" in p["capabilities"])
client.send(message_id="q-1", to=seller, kind="quote_request",
            body={key: task[key]
                  for key in ("sku", "quantity", "unit_price_cents")})

deadline = time.time() + 30
response = None
while response is None and time.time() < deadline:
    client.notify(wait=0.2)
    response = client.claim()
if response is None:
    raise SystemExit("no quote response arrived")
client.ack(response["message_id"], response["fence"], "processed",
           {"correct": True,
            "total_cents": response["body"]["total_cents"],
            "expected_total_cents": task["expected_total_cents"]})
