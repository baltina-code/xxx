"""CR-A: iedzīvotājs atsauc iesniegumu (tracker/CR-A.md)."""

import logging
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import clock, storage
from app.main import app

REASON = "Problēma jau ir atrisināta"
# Sākuma dati (README): ID pēc statusa.
RECEIVED = "IES-2026-000001"
IN_PROGRESS = "IES-2026-000002"
ANSWERED = "IES-2026-000003"
FORWARDED = "IES-2026-000004"
WITHDRAWN = "IES-2026-000005"
UNKNOWN = "IES-2026-999999"


@pytest.fixture
def seeded(client):
    storage.reset()
    return client


def withdraw(client, submission_id, reason=REASON):
    return client.post(
        f"/submissions/{submission_id}/withdraw", json={"reason": reason}
    )


def status_of(client, submission_id):
    return client.get(f"/submissions/{submission_id}").json()["status"]


def personal_data(submission_id):
    record = storage.get(submission_id)
    return [record[key] for key in ("personalCode", "fullName", "email", "body")]


def test_cra_ac1_received_withdrawn(seeded):
    due_before = seeded.get(f"/submissions/{RECEIVED}").json()["dueDate"]

    response = withdraw(seeded, RECEIVED)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == RECEIVED
    assert body["status"] == "WITHDRAWN"
    # Precizējums: dueDate pēc atsaukšanas nemainās.
    assert body["dueDate"] == due_before
    assert status_of(seeded, RECEIVED) == "WITHDRAWN"


def test_cra_ac2_in_progress_withdrawn(seeded):
    response = withdraw(seeded, IN_PROGRESS, "Jautājums vairs nav aktuāls")

    assert response.status_code == 200
    assert response.json()["status"] == "WITHDRAWN"
    assert status_of(seeded, IN_PROGRESS) == "WITHDRAWN"


def test_cra_ac3_answered_409(seeded):
    response = withdraw(seeded, ANSWERED)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"
    assert status_of(seeded, ANSWERED) == "ANSWERED"


def test_cra_ac4_second_withdraw_409(seeded):
    assert withdraw(seeded, RECEIVED).status_code == 200

    response = withdraw(seeded, RECEIVED)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"
    actions = [e["action"] for e in seeded.get(f"/submissions/{RECEIVED}/audit").json()]
    assert actions.count("WITHDRAW") == 1


def test_cra_ac4_seeded_withdrawn_409(seeded):
    response = withdraw(seeded, WITHDRAWN)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"


def test_cra_forwarded_409_assumption(seeded):
    # Atvērtais jautājums: pieņēmums, ka FORWARDED atsaukt nevar (līgumā atļauti
    # tikai RECEIVED un IN_PROGRESS). Sk. PR piezīmi.
    response = withdraw(seeded, FORWARDED)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"
    assert status_of(seeded, FORWARDED) == "FORWARDED"


def test_cra_ac5_unknown_id_404(seeded):
    response = withdraw(seeded, UNKNOWN)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"reason": None},
        {"reason": ""},
        {"reason": "a" * 9},
        {"reason": "a" * 501},
        # Atstarpes sākumā un beigās neskaita: saturīgas ir 3 rakstzīmes.
        {"reason": "   abc    "},
    ],
    ids=["missing", "null", "empty", "9-chars", "501-chars", "padded-3-chars"],
)
def test_cra_ac6_invalid_reason_400(seeded, payload):
    response = seeded.post(f"/submissions/{RECEIVED}/withdraw", json=payload)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert [d["field"] for d in error["details"]] == ["reason"]
    assert status_of(seeded, RECEIVED) == "RECEIVED"


def test_cra_ac6_no_body_400(seeded):
    response = seeded.post(f"/submissions/{RECEIVED}/withdraw")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == [{"field": "reason", "issue": "REQUIRED"}]
    assert status_of(seeded, RECEIVED) == "RECEIVED"


@pytest.mark.parametrize("length", [10, 500])
def test_cra_ac6_boundary_lengths_accepted(seeded, length):
    assert withdraw(seeded, RECEIVED, "a" * length).status_code == 200


def test_cra_ac7_audit_withdraw_entry(seeded, monkeypatch):
    fixed = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(clock, "now", lambda: fixed)

    withdraw(seeded, RECEIVED, f"  {REASON}  ")
    entries = seeded.get(f"/submissions/{RECEIVED}/audit").json()

    assert {"at": "2026-10-05T12:00:00Z", "action": "WITHDRAW", "detail": REASON} in (
        entries
    )


def test_cra_ac8_no_personal_data_in_log_or_errors(seeded, caplog):
    caplog.set_level(logging.DEBUG)
    secrets_ = personal_data(RECEIVED) + personal_data(ANSWERED)

    responses = [
        seeded.post(f"/submissions/{RECEIVED}/withdraw", json={"reason": "short"}),
        withdraw(seeded, ANSWERED),
        withdraw(seeded, UNKNOWN),
        withdraw(seeded, RECEIVED),
        withdraw(seeded, RECEIVED),
    ]
    errors = [r.text for r in responses if r.status_code >= 400]

    assert len(errors) == 4
    for value in secrets_:
        assert value not in caplog.text
        assert all(value not in text for text in errors)


def test_cra_ac8_unexpected_error_hides_details(seeded, monkeypatch, caplog):
    leaked = personal_data(RECEIVED)

    def broken(_submission_id):
        raise RuntimeError(f"db failure for {' '.join(leaked)}")

    monkeypatch.setattr(storage, "get", broken)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(f"/submissions/{RECEIVED}/withdraw", json={"reason": REASON})

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "INTERNAL_ERROR"
    assert "atbalsta dienest" in error["message"]
    reference = error["message"].rstrip(".").split()[-1]
    assert reference.startswith("ERR-")
    assert reference in caplog.text
    for value in leaked:
        assert value not in response.text
        assert value not in caplog.text
