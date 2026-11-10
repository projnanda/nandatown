"""Validators for the Bazaroo scenarios. Loaded through `plugin_files:` in the YAML.

They judge only what the plugin recorded as evidence (event kinds emitted by
registry_bazaroo_v1.py), so a PASS means "Bazaroo made these decisions and Town
passed them through", not that Bazaroo itself is correct in general.

Town's _check(name, ok, evidence, fail_note) reports "missing" (inconclusive) when the
evidence list is empty, so each check also attaches the events that would show a
failure; a failure then reads as FAILED with evidence, not as inconclusive.
"""
from nandatown.sim.validators import _check, validator


def _ids(trace, *kinds):
    out = []
    for kind in kinds:
        out.extend(trace.ids(kind))
    return list(out)


@validator("bazaroo-discovery")
def bazaroo_discovery(spec, trace):
    accepted = _ids(trace, "bazaroo_card_accepted")
    refused = _ids(trace, "card_publish_refused")
    hits = _ids(trace, "bazaroo_lookup_hit")
    misses = _ids(trace, "bazaroo_lookup_empty", "registry_lookup_failed")
    return [
        _check("bazaroo_registration", bool(accepted) and not refused,
               accepted + refused,
               "an agent was refused by Bazaroo, or none was accepted",
               "Bazaroo accepted every agent's card"),
        _check("bazaroo_discovery", bool(hits) and not misses,
               hits + misses,
               "the buyer could not discover the seller through Bazaroo",
               "the buyer found the seller through Bazaroo"),
    ]


@validator("bazaroo-name-taken")
def bazaroo_name_taken(spec, trace):
    taken = _ids(trace, "bazaroo_name_taken")
    accepted = _ids(trace, "bazaroo_card_accepted")
    hits = _ids(trace, "bazaroo_lookup_hit")
    misses = _ids(trace, "bazaroo_lookup_empty", "registry_lookup_failed")
    return [
        _check("bazaroo_refuses_taken_name", bool(taken),
               taken or accepted,
               "Bazaroo did not refuse a name that another key already owns",
               "Bazaroo refused a name another key owns (409 name_taken)"),
        _check("real_listing_survives", bool(hits) and not misses,
               hits + misses,
               "after the refusal, the original listing could no longer be found",
               "the original listing was still discoverable after the refusal"),
    ]
