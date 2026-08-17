"""Phase 94: whose box is this, and is it actually idle.

THE INCIDENT. Three agent sessions shared one Lambda account through one
shared MCP token. One of them listed instances, found an A100 it had not
launched, checked it the only ways it could - uptime, logged-in users,
processes, writes to the NFS - and concluded it was a stray. It was a
vLLM box six minutes into loading Qwen3.5-27B from the shared HF cache:
no users, no obvious processes, nothing written, 30GB of VRAM held. It
terminated it about sixty seconds before the model would have served. The
note it left reads "Verified idle before terminating: up 6 min, 0 users, no
user processes, nothing written to the NFS."

(An earlier version of this docstring also blamed that termination for a
multi-hour extraction run dying the same morning. It did not: that run was
killed at 07:36:56 by Manifold's own idle sweep, which reaped a box its own
telemetry had just recorded at 100% GPU utilization and 36GB of VRAM held.
Same error, one layer down. Kept here because assuming a second failure
shared a cause with the first is exactly the reflex these tests exist to
discourage.)

Nothing in that reasoning was careless. The API had told it everything it
asked, and everything it asked was the wrong question, because:

  - GET /instances carried launch_id and NOTHING about who launched it or
    what for, so an in-use box and an abandoned one look identical.
  - The dispatcher's own idle sweep ALREADY knew the difference (Phase 90
    protects a server that is not answering yet, precisely so a 70B is not
    reaped at minute 30 for the crime of still loading). That verdict was
    computed every sweep and thrown away; readers got idle_seconds and had
    to reinvent it from shell commands.

So this pins three things: the payload says whose and what for, the
payload says what the sweep concluded and why, and terminate refuses
another principal's instance instead of trusting six sessions to each
remember a convention.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import Guardrails
from app.main import create_app
from tests.conftest import (make_settings, mock_connect_fn,
                            wait_for_launch_status)
from tests.test_idle_matrix import NOW, TIMEOUT, Harness

OWNER = "owner-token-for-tests"

LAUNCH = {"instance_type": "gpu_1x_a10", "region": "us-east-1",
          "filesystem": "manifold-data"}


@pytest.fixture
def owner(tmp_path, mock_client, mock_storage, mock_sidecar, mock_model):
    """A token-enforcing app with room for two principals to both launch.

    The default guardrails cap concurrency at 1, which would refuse the
    second launch and prove nothing about ownership.
    """
    app = create_app(
        make_settings(tmp_path, api_token=OWNER,
                      guardrails=Guardrails(max_concurrent_instances=5,
                                            max_hourly_spend_usd=10.0)),
        lambda_client=mock_client,
        storage_factory=lambda fs: mock_storage,
        connect_fn=mock_connect_fn,
        sidecar_factory=lambda conn: mock_sidecar,
        env_path=tmp_path / ".env",
    )
    with TestClient(app, headers={"Authorization": f"Bearer {OWNER}"}) as c:
        yield c


def principal(owner, name):
    """Mint a named principal and return a client authenticated as it."""
    resp = owner.post("/principals", json={"name": name, "role": "operator"})
    assert resp.status_code == 201, resp.text
    c = TestClient(owner.app)
    c.headers.update({"Authorization": f"Bearer {resp.json()['token']}"})
    return c


def launch(client, **extra):
    """Launch and return the instance id once the provider has accepted."""
    resp = client.post("/instances", json={**LAUNCH, **extra})
    assert resp.status_code == 202, resp.text
    row = wait_for_launch_status(client, resp.json()["launch"]["id"])
    return row["lambda_instance_id"]


def instance(client, instance_id):
    return next(i for i in client.get("/instances").json()["instances"]
                if i["id"] == instance_id)


def detach(client, instance_id):
    """Drop the SSH supervisor before a termination we expect to SUCCEED.

    Nothing to do with the guard: these tests drive one app through two
    TestClients (one per principal), and only the fixture's client owns the
    running loop. A real terminate then tries to await the supervisor task
    from the other client's loop and raises "attached to a different loop" -
    an artifact of the harness, not of the code under test. The ownership
    check runs before the rescue and before this close, so dropping the
    connection removes the flake without removing any of the assertion.
    """
    client.app.state.orchestrator.connections.pop(instance_id, None)


# -- the payload: whose, and what for -----------------------------------------


def test_the_instance_list_says_who_launched_it(owner):
    """The field whose absence caused the incident."""
    tally = principal(owner, "tally")
    iid = launch(tally, purpose="Tally extraction+evaluation run")

    inst = instance(owner, iid)
    assert inst["created_by"] == "tally"
    assert inst["purpose"] == "Tally extraction+evaluation run"


def test_another_principal_sees_the_same_attribution(owner):
    """Attribution is worthless if only the owner can read it - the reader
    who needs it is by definition someone else."""
    tally = principal(owner, "tally")
    redhope = principal(owner, "red-hope")
    iid = launch(tally, purpose="Tally extraction+evaluation run")

    inst = instance(redhope, iid)
    assert inst["created_by"] == "tally"
    assert inst["purpose"] == "Tally extraction+evaluation run"


def test_a_box_with_no_stated_purpose_reads_as_unattributed(owner):
    """None, not "" and not a guess. A reader must be able to tell "nobody
    said" from "said it is nothing", because only one of those invites a
    question before acting."""
    tally = principal(owner, "tally")
    iid = launch(tally)

    inst = instance(owner, iid)
    assert inst["created_by"] == "tally"
    assert inst["purpose"] is None


def test_purpose_survives_into_the_launch_row(owner):
    """It has to outlive the request: the reader arrives hours later."""
    tally = principal(owner, "tally")
    iid = launch(tally, purpose="warming a 27B, back in 12 min")
    row = owner.app.state.orchestrator.db.find_launch_by_instance(iid)
    assert row["purpose"] == "warming a 27B, back in 12 min"
    assert row["created_by"] == "tally"


# -- the guard: not yours, not your call --------------------------------------


def test_terminating_another_principals_instance_is_refused(owner):
    """The whole point. tally's box, red-hope asking."""
    tally = principal(owner, "tally")
    redhope = principal(owner, "red-hope")
    iid = launch(tally, purpose="Tally extraction+evaluation run")

    resp = redhope.delete(f"/instances/{iid}")

    assert resp.status_code == 409
    body = resp.json()
    assert body["refused"] is True
    assert body["owner"] == "tally"
    # The refusal must carry enough to END the question, not just block it.
    assert body["purpose"] == "Tally extraction+evaluation run"
    assert body["override"] == {"confirm_owner": "tally"}
    # And the instance is still there.
    assert instance(owner, iid)["id"] == iid


def test_the_refusal_names_the_owner_in_its_message(owner):
    """A caller that cannot see whose it is will try again rather than ask.
    That is how the second termination happened."""
    tally = principal(owner, "tally")
    redhope = principal(owner, "red-hope")
    iid = launch(tally, purpose="Tally extraction+evaluation run")

    detail = redhope.delete(f"/instances/{iid}").json()["detail"]
    assert "tally" in detail
    assert "confirm_owner" in detail
    assert "Tally extraction+evaluation run" in detail


def test_confirm_owner_overrides_it(owner):
    """Deliberate cross-owner termination stays possible - the guard is
    against acting without looking, not against acting."""
    tally = principal(owner, "tally")
    redhope = principal(owner, "red-hope")
    iid = launch(tally)
    detach(owner, iid)

    resp = redhope.delete(f"/instances/{iid}?confirm_owner=tally")
    assert resp.status_code == 200, resp.text
    assert resp.json()["terminated"] is True


def test_the_wrong_owner_name_does_not_override(owner):
    """confirm_owner is proof you read the record. A value that does not
    match is proof you did not."""
    tally = principal(owner, "tally")
    redhope = principal(owner, "red-hope")
    iid = launch(tally)

    resp = redhope.delete(f"/instances/{iid}?confirm_owner=someone-else")
    assert resp.status_code == 409
    assert resp.json()["owner"] == "tally"


def test_force_does_not_bypass_ownership(owner):
    """THE design decision, pinned. force=true means "burn it, I accept the
    data loss" and skips the rescue. If it also waived ownership, the single
    call for taking another principal's box would be the one call that
    destroys their unsaved files without looking first."""
    tally = principal(owner, "tally")
    redhope = principal(owner, "red-hope")
    iid = launch(tally)

    resp = redhope.delete(f"/instances/{iid}?force=true")
    assert resp.status_code == 409
    assert resp.json()["refused"] is True
    assert instance(owner, iid)["id"] == iid, "force destroyed another's box"


def test_your_own_instance_terminates_normally(owner):
    """The guard must be invisible to the common case."""
    tally = principal(owner, "tally")
    iid = launch(tally)
    detach(owner, iid)

    resp = tally.delete(f"/instances/{iid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["terminated"] is True


def test_an_unattributed_instance_is_not_guarded(owner):
    """A box with no recorded owner (adopted, or launched before this
    shipped) cannot be protected by a rule about owners. Refusing on an
    unknown owner would teach callers to pass confirm_owner reflexively,
    which is worse than not guarding: it trains away the pause."""
    iid = launch(owner)          # the .env token: created_by "owner"
    owner.app.state.orchestrator.db._execute(
        "UPDATE launches SET created_by = NULL WHERE lambda_instance_id = ?",
        (iid,))
    redhope = principal(owner, "red-hope")
    detach(owner, iid)

    assert redhope.delete(f"/instances/{iid}").status_code == 200


def test_a_refusal_is_audited(owner):
    """The incident review could not tell which session did what. A refusal
    is exactly the event worth having a row for."""
    tally = principal(owner, "tally")
    redhope = principal(owner, "red-hope")
    iid = launch(tally)

    redhope.delete(f"/instances/{iid}")

    rows = owner.get("/audit").json()["entries"]
    refused = [r for r in rows if r["action"] == "terminate_refused"]
    assert len(refused) == 1
    assert refused[0]["actor"] == "red-hope"
    assert "tally" in refused[0]["detail"]


def test_an_override_is_audited_too(owner):
    """Allowed is not the same as unremarkable: one principal destroying
    another's box is precisely what the review needed and could not find."""
    tally = principal(owner, "tally")
    redhope = principal(owner, "red-hope")
    iid = launch(tally)
    detach(owner, iid)

    redhope.delete(f"/instances/{iid}?confirm_owner=tally")

    rows = owner.get("/audit").json()["entries"]
    crossed = [r for r in rows if r["action"] == "terminate_across_owners"]
    assert len(crossed) == 1
    assert crossed[0]["actor"] == "red-hope"


async def test_the_idle_sweep_is_never_ownership_checked(tmp_path, db):
    """Load-bearing: the sweep terminates on the SYSTEM's behalf, not a
    principal's. If it inherited a caller it would refuse to reap anyone's
    box but its own launcher's, and idle auto-termination - the feature this
    product exists for - would silently stop working the moment James issued
    a second token."""
    harness = Harness(tmp_path, db)
    launch_id = harness.add_instance("i-someone-elses")
    harness.db._execute(
        "UPDATE launches SET created_by = 'tally' WHERE id = ?", (launch_id,))

    await harness.dispatcher._check_idle()

    assert [i for i, _ in harness.terminated] == ["i-someone-elses"]
    assert all("caller" not in kw for _, kw in harness.terminated), (
        "the sweep passed a principal and opted itself into the guard")


# -- the verdict: is it ACTUALLY idle -----------------------------------------


@pytest.fixture
def harness(tmp_path, db):
    return Harness(tmp_path, db)


def ready(harness, is_ready: bool):
    async def _probe(instance_id, task_id, port):
        return {"ready": is_ready, "error": "" if is_ready else "loading"}
    harness.dispatcher.model_ready = _probe


async def test_a_loading_server_reports_itself_as_loading(harness):
    """THE case that cost the run. Every signal the terminating agent could
    reach said idle; the sweep knew it was loading and said nothing."""
    harness.add_instance("i-warming")
    harness.pin_task("i-warming", "vllm-serve")
    ready(harness, False)

    await harness.dispatcher._check_idle()

    status = harness.dispatcher.activity_status("i-warming")
    assert status["state"] == "loading"
    assert status["busy"] is True
    assert "not answering yet" in status["reason"]
    assert harness.terminated == []


async def test_a_serving_box_is_busy_even_between_requests(harness):
    """A model that is loaded and answering is doing something, even in a
    quiet second. Reporting busy=false between requests is how a reader
    talks itself into terminating a live endpoint."""
    harness.add_instance("i-serving", idle_for=5)
    harness.pin_task("i-serving", "vllm-serve")
    ready(harness, True)

    await harness.dispatcher._check_idle()

    status = harness.dispatcher.activity_status("i-serving")
    assert status["state"] == "serving"
    assert status["busy"] is True
    assert harness.terminated == []


async def test_a_batch_job_reports_the_job(harness):
    harness.add_instance("i-batch")
    harness.pin_task("i-batch", "axolotl-finetune")

    await harness.dispatcher._check_idle()

    status = harness.dispatcher.activity_status("i-batch")
    assert status["state"] == "batch_running"
    assert status["busy"] is True


async def test_a_genuinely_quiet_box_says_so(harness):
    """The guard must not cry wolf: a box with nothing on it reports idle,
    or the field is noise and readers learn to skip it."""
    harness.add_instance("i-quiet", idle_for=TIMEOUT / 2)

    await harness.dispatcher._check_idle()

    status = harness.dispatcher.activity_status("i-quiet")
    assert status["state"] == "idle_countdown"
    assert status["busy"] is False


async def test_an_unreachable_box_reports_unknown_not_idle(harness):
    """busy=None, and it must never be read as false. We cannot see inside a
    box we cannot reach, and "no evidence of work" is not "evidence of no
    work" - that inference is the whole bug."""
    harness.add_instance("i-gone", connected=False)

    await harness.dispatcher._check_idle()

    status = harness.dispatcher.activity_status("i-gone")
    assert status["state"] == "unreachable"
    assert status["busy"] is None


def test_an_unjudged_instance_is_unknown_not_idle(harness):
    """Before the first sweep there is no verdict, and inventing a
    reassuring one is how a reader gets told a warming box is free."""
    status = harness.dispatcher.activity_status("i-never-seen")
    assert status["state"] == "unknown"
    assert status["busy"] is None
    assert status["age_seconds"] is None


@pytest.mark.parametrize("status", ["launching", "retrying", "booting"])
def test_a_booting_instance_says_booting_not_unknown(harness, status):
    """The first box destroyed in the incident was still BOOTING. It has no
    SSH connection, so the sweep has never seen it and could only say
    "unknown" - and "unknown" is what a reader talks itself past. The launch
    row answers it for free, since the caller already holds the row."""
    result = harness.dispatcher.activity_status(
        "i-coming-up", {"status": status})
    assert result["state"] == "booting"
    assert result["busy"] is True
    assert status in result["reason"]


@pytest.mark.parametrize("status", ["active", "terminated", "failed"])
def test_a_settled_launch_does_not_claim_to_be_booting(harness, status):
    """The guard on the guard: only the pre-active statuses mean "coming
    up". A terminated or failed launch reporting busy=true would protect
    boxes that no longer exist and make the field noise."""
    result = harness.dispatcher.activity_status(
        "i-settled", {"status": status})
    assert result["state"] == "unknown"
    assert result["busy"] is None


async def test_a_real_verdict_beats_the_launch_row(harness):
    """Once the sweep has actually looked, what it SAW wins over what the
    launch row implies. A row that still says booting while the box is up
    and idle must not keep it protected forever."""
    harness.add_instance("i-up", idle_for=TIMEOUT / 2)
    await harness.dispatcher._check_idle()

    result = harness.dispatcher.activity_status("i-up", {"status": "booting"})
    assert result["state"] == "idle_countdown"


async def test_a_terminated_box_stops_reporting_serving(harness):
    """A verdict that outlives its instance is a lie waiting to be read."""
    # Inside its window, so the sweep records a verdict instead of reaping it
    # on the same pass (which would leave nothing to prove was cleaned up).
    harness.add_instance("i-doomed", idle_for=TIMEOUT / 2)
    await harness.dispatcher._check_idle()
    assert harness.dispatcher.activity_status("i-doomed")["state"] != "unknown"

    await harness.dispatcher._terminate_for("i-doomed", "idle", "test")

    assert harness.dispatcher.activity_status("i-doomed")["state"] == "unknown"


def test_the_verdict_reaches_the_instances_payload(owner):
    """The unit tests above prove the dispatcher knows. This proves it
    actually leaves the dispatcher, which is the entire complaint."""
    iid = launch(owner)
    assert "activity" in instance(owner, iid)
    assert "state" in instance(owner, iid)["activity"]
