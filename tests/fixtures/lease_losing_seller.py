"""The stock seller, slow enough once to lose its lease.

A test fixture, not an independent agent. Its first acknowledgement that
records an application waits past the lease, so the town fences it and
redelivers the request, as it would to a real seller on a slow host.
"""
import time

from nandatown.client import TownClient
from nandatown.participants import seller

_ack = TownClient.ack
_stalled = []


def _slow_ack(self, message_id, fence, status, note=None):
    if not _stalled and (note or {}).get("applied"):
        _stalled.append(True)
        time.sleep(float(self.run_context.get("lease_seconds", 5.0)) + 0.7)
    return _ack(self, message_id, fence, status, note)


TownClient.ack = _slow_ack
seller.main()
