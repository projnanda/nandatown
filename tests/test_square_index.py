import re

import pytest

from nandatown.square.index_client import NandaIndexClient, NandaIndexError
from nandatown.square.local_index import LocalNandaIndex

PASSWORD = "correct-horse-battery"


@pytest.fixture()
def index():
    fake = LocalNandaIndex()
    return fake.client(), fake


def register(client, org_id, display_name, email):
    token = client.account_token(email, PASSWORD)
    return client.register_agent(
        token, org_id=org_id, display_name=display_name,
        contact_email=email,
        card_url=f"https://agents.test/{org_id}/agent-card.json",
        description="personal agent", tags=["town-square"])


def test_registration_by_agent_email_starts_pending(index):
    client, fake = index
    reg = register(client, "instinct-a", "Instinct A", "a@agents.test")
    assert reg.status == "pending" and not reg.discoverable
    sent = fake.orgs["instinct-a"]
    assert sent["hosting_path"] == "personal"
    assert sent["contact_email"] == "a@agents.test"
    assert sent["registry_url"].endswith("/instinct-a/agent-card.json")
    assert sent["media_type"] == "application/a2a-agent-card+json"
    assert client.search("instinct") == []


def test_email_verification_makes_the_agent_discoverable(index):
    client, _ = index
    register(client, "instinct-a", "Instinct A", "a@agents.test")
    reg = client.verify_email("verify-instinct-a")
    assert reg.discoverable
    assert [r["org_id"] for r in client.search("instinct")] == ["instinct-a"]


def test_agents_find_their_peers_but_not_themselves(index):
    client, _ = index
    for org, name, email in [("instinct-a", "Square Instinct", "a@x.test"),
                             ("muse-b", "Square Muse", "b@x.test"),
                             ("openclaw-c", "Square OpenClaw", "c@x.test")]:
        register(client, org, name, email)
        client.verify_email(f"verify-{org}")
    peers = client.find_peers("square", me="instinct-a")
    assert [p["org_id"] for p in peers] == ["muse-b", "openclaw-c"]


def test_an_existing_account_logs_in_instead(index):
    client, fake = index
    first = client.account_token("a@agents.test", PASSWORD)
    again = client.account_token("a@agents.test", PASSWORD)
    assert first == again
    assert fake.calls[-1] == ("POST", "/auth/login")


def test_a_wrong_password_fails_without_echoing_it(index):
    client, _ = index
    client.account_token("a@agents.test", PASSWORD)
    with pytest.raises(NandaIndexError) as err:
        client.account_token("a@agents.test", "wrong-password-123")
    assert "wrong-password-123" not in str(err.value)
    assert "401" in str(err.value)


def test_a_taken_org_id_is_reported(index):
    client, _ = index
    register(client, "instinct-a", "Instinct A", "a@agents.test")
    with pytest.raises(NandaIndexError, match="taken"):
        register(client, "instinct-a", "Other", "z@agents.test")


def test_an_unknown_verification_token_is_refused(index):
    client, _ = index
    with pytest.raises(NandaIndexError, match="404"):
        client.verify_email("verify-nobody")


@pytest.mark.parametrize("field, value, message", [
    ("org_id", "Instinct_A", "org_id"),
    ("card_url", "http://agents.test/card.json", "https"),
    ("contact_email", "not-an-email", "email"),
])
def test_bad_registrations_are_refused_before_any_request(index, field,
                                                          value, message):
    client, fake = index
    args = {"org_id": "instinct-a", "display_name": "Instinct A",
            "contact_email": "a@agents.test",
            "card_url": "https://agents.test/card.json", field: value}
    with pytest.raises(NandaIndexError, match=message):
        client.register_agent("jwt:a@agents.test", **args)
    assert fake.calls == []


def test_server_errors_surface_as_index_errors(index):
    client, fake = index
    fake.fail_with = 500
    with pytest.raises(NandaIndexError, match="500"):
        client.search("anything")


def test_a_bad_password_is_refused_locally(index):
    client, fake = index
    with pytest.raises(NandaIndexError, match="8"):
        client.account_token("a@agents.test", "short")
    assert fake.calls == []


@pytest.mark.parametrize("url", ["http://api.nandaindex.org",
                                 "ftp://index.test", "not a url"])
def test_the_index_must_be_https(url):
    with pytest.raises(NandaIndexError, match="https"):
        NandaIndexClient(url)


def test_plain_http_is_allowed_for_a_local_index():
    client = NandaIndexClient("http://localhost:3000")
    assert client.base_url == "http://localhost:3000"


def test_the_default_is_the_live_index():
    assert re.fullmatch(r"https://api\.nandaindex\.org",
                        NandaIndexClient().base_url)


def test_the_verification_link_goes_to_the_agent_inbox(index):
    client, fake = index
    register(client, "muse-b", "Muse B", "b@agents.test")
    assert fake.outbox == {"b@agents.test": "verify-muse-b"}
    assert fake.users["b@agents.test"] != PASSWORD
