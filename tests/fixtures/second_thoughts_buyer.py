"""A buyer that acknowledges the quote provisionally, then decides.

A test fixture, not an independent agent. argv[1] is the provisional
correctness assertion it sends with a `retryable` acknowledgement, and
argv[2] the one it sends with its terminal acknowledgement, each as
"true", "false" or "none" for no assertion.
"""
import os
import sys
import time

from nandatown.client import TownClient

READ = {"true": True, "false": False, "none": None}
provisional, terminal = READ[sys.argv[1]], READ[sys.argv[2]]

client = TownClient(os.environ["TOWN_URL"], os.environ["RUN_ID"])
client.join_auto(os.environ["NAME"], os.environ["TOKEN"], None)
task = client.run_context["task"]
seller = next(p["name"] for p in client.participants()
              if "quote.read" in p["capabilities"])
client.send(message_id="q-1", to=seller, kind="quote_request",
            body={key: task[key]
                  for key in ("sku", "quantity", "unit_price_cents")})


def claim():
    deadline = time.time() + 30
    while time.time() < deadline:
        client.notify(wait=0.2)
        got = client.claim()
        if got:
            return got
    raise SystemExit("no quote response arrived")


def note(correct, response):
    body = {"total_cents": response["body"]["total_cents"]}
    if correct is not None:
        body["correct"] = correct
    return body


first = claim()
client.ack(first["message_id"], first["fence"], "retryable",
           note(provisional, first))
second = claim()
client.ack(second["message_id"], second["fence"], "processed",
           note(terminal, second))
