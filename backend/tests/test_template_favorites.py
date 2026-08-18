"""Phase 107: favorite templates lead every template list.

The quick-jobs picker passed twenty entries (owner report, screenshot of
the scroll): finding YOUR job became a scan. Favorites are a preference
(SQLite via PUT /preferences), the ORDER is decided once in the backend's
/templates route, and every client - the dashboard select, the MCP
list_templates tool - inherits it without re-implementing the sort.
"""


def names(client) -> list[str]:
    return [t["name"] for t in client.get("/templates").json()["templates"]]


def set_favorites(client, favorites):
    resp = client.put("/preferences", json={"templates":
                                            {"favorites": favorites}})
    assert resp.status_code == 200, resp.text
    return resp.json()["preferences"]["templates"]["favorites"]


def test_no_favorites_means_the_old_order_and_no_flags(client):
    body = client.get("/templates").json()["templates"]
    assert all(t["favorite"] is False for t in body)


def test_favorites_lead_in_stored_order(client):
    base = names(client)
    picks = [base[5], base[2]]          # deliberately not alphabetical
    assert set_favorites(client, picks) == picks
    got = names(client)
    assert got[:2] == picks, "favorites must lead, in the user's order"
    # The remainder keeps its existing order exactly (stable sort).
    assert got[2:] == [n for n in base if n not in picks]
    flags = {t["name"]: t["favorite"]
             for t in client.get("/templates").json()["templates"]}
    assert flags[picks[0]] and flags[picks[1]]
    assert sum(flags.values()) == 2


def test_unfavorite_restores(client):
    base = names(client)
    set_favorites(client, [base[3]])
    set_favorites(client, [])
    assert names(client) == base


def test_a_favorite_for_a_missing_template_is_kept_but_invisible(client):
    """Deleting a template must not silently edit the user's preferences;
    a name with no matching template simply does not render."""
    base = names(client)
    stored = set_favorites(client, ["ghost-template", base[0]])
    assert stored == ["ghost-template", base[0]]   # preference intact
    got = names(client)
    assert got[0] == base[0]
    assert "ghost-template" not in got


def test_junk_is_dropped_and_capped(client):
    stored = set_favorites(
        client, ["  vllm-serve  ", "vllm-serve", "", "gpu-smoke"])
    assert stored == ["vllm-serve", "gpu-smoke"]   # trimmed, deduped
    resp = client.put("/preferences",
                      json={"templates": {"favorites": [f"t{i}" for i in range(80)]}})
    assert len(resp.json()["preferences"]["templates"]["favorites"]) == 50


def test_round_trip_survives_reload(client):
    set_favorites(client, ["gpu-smoke"])
    prefs = client.get("/preferences").json()["preferences"]
    assert prefs["templates"]["favorites"] == ["gpu-smoke"]
