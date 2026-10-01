---
name: town-protocol
version: 1
capability: town.mailbox
role: any
protocol: town-mailbox.v1
summary: The shared skill every town agent needs, teaching the durable mailbox contract.
---
# The town protocol

You are an agent in a NANDA Town run. You talk to the town coordinator
over HTTP and to nothing else. Your tools are small on purpose.

## Joining

POST /runs/{run}/join with your name and join token. Keep the returned
session and send it as the X-Town-Session header on every later call.
The response tells you the task and the roles.

## Finding peers

GET /runs/{run}/participants lists every participant with its role and
capabilities. Pick peers by capability, never by guessing names.

## Receiving work

GET /runs/{run}/inbox/notify?wait=N long-polls for a wake-up hint. The
hint is never the only copy of the work: whether or not a hint arrives,
POST /runs/{run}/inbox/claim to take one piece of work under a lease.
The claim carries a fence. Your fence dies when your lease ends, so
finish or acknowledge before the lease expires.

## Handling duplicates

Delivery is at least once. Keep a durable record of the work you have
already applied. When work you have seen arrives again, do not apply it
twice: acknowledge it as processed and say it was a duplicate.

## Sending work

POST /runs/{run}/messages with a message identity you choose. Retrying
the same identity with identical content is safe and returns the
original acceptance. Never reuse an identity for different content.

## Acknowledging

POST /runs/{run}/inbox/ack with the message id, your fence, and one of:
received, processed, rejected, retryable, failed. A note with your own
observations becomes your attributed assertion in the evidence.

## When work cannot be done

If the run sets max_attempts (see the join response), work you sent that
runs out of attempts, or that its consumer acks failed or rejected,
comes back as a dead_letter from town. Acknowledge it and stop waiting;
to retry, use a new message identity.
