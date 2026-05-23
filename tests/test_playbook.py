from __future__ import annotations


def test_playbook_pending_not_injected_until_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("WLWL_PLAYBOOK_PATH", str(tmp_path / "playbook.json"))

    from launcher import playbook

    entry = playbook.propose(
        "When searching code, use rg before slower recursive scans.",
        category="coding",
        rationale="Verified in local repository work.",
    )

    assert entry["status"] == "pending"
    assert playbook.render_prompt() == ""

    ok, msg = playbook.accept(entry["id"], note="reviewed")

    assert ok is True
    assert msg == "reviewed"
    rendered = playbook.render_prompt()
    assert "Reviewed execution lessons" in rendered
    assert "use rg before slower recursive scans" in rendered


def test_curator_propose_playbook_writes_review_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("WLWL_PLAYBOOK_PATH", str(tmp_path / "playbook.json"))
    monkeypatch.setenv("WLWL_CURATOR_PROPOSALS_PATH", str(tmp_path / "curator.jsonl"))

    from launcher import playbook
    from tools.curator_propose import curator_propose, list_proposals

    message = curator_propose(
        "After editing tool schemas, validate both JSON files.",
        target="playbook",
        rationale="Invalid schema breaks startup.",
        source_turn=7,
        source_session="test-session",
    )

    assert "Playbook proposal" in message
    proposals = list_proposals()
    assert proposals[-1]["target"] == "playbook"
    assert proposals[-1]["source_turn"] == 7

    entries = playbook.list_entries(status="pending")
    assert len(entries) == 1
    assert entries[0]["source"] == "curator_propose"
    assert entries[0]["source_turn"] == 7
    assert playbook.render_prompt() == ""


def test_playbook_api_routes_create_and_decide(tmp_path, monkeypatch):
    monkeypatch.setenv("WLWL_PLAYBOOK_PATH", str(tmp_path / "playbook.json"))

    from launcher import api_server

    status, payload = api_server._route_playbook_create(
        {
            "body": {
                "content": "For risky filesystem edits, prefer reviewed local patches.",
                "category": "safety",
                "rationale": "Keeps unrelated worktree changes intact.",
            }
        }
    )

    assert status == 200
    entry_id = payload["entry"]["id"]

    status, payload = api_server._route_playbook_list({"query": {"status": "pending"}})

    assert status == 200
    assert payload["stats"]["status"]["pending"] == 1

    status, payload = api_server._route_playbook_decide(
        {
            "params": {"entry_id": entry_id},
            "body": {"decision": "accept", "note": "approved"},
        }
    )

    assert status == 200
    assert payload == {"ok": True, "message": "approved"}

    status, payload = api_server._route_playbook_list({"query": {"status": "active"}})

    assert status == 200
    assert payload["entries"][0]["id"] == entry_id
