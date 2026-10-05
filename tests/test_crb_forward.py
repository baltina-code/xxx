"""CR-B: darbinieks pārsūta iesniegumu citai iestādei (tracker/CR-B.md)."""

import logging
from datetime import datetime, timezone

import pytest

from app import clock, storage
from app.main import is_forwarded_late

# Sākuma dati (README): ID pēc statusa.
RECEIVED = "IES-2026-000001"
IN_PROGRESS = "IES-2026-000002"
ANSWERED = "IES-2026-000003"
FORWARDED = "IES-2026-000004"
WITHDRAWN = "IES-2026-000005"
UNKNOWN = "IES-2026-999999"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def seeded(client, monkeypatch):
    storage.reset()
    monkeypatch.setattr(clock, "now", lambda: NOW)
    return client


def forward(client, submission_id, code="VCD"):
    return client.post(
        f"/submissions/{submission_id}/forward", json={"institutionCode": code}
    )


def status_of(client, submission_id):
    return client.get(f"/submissions/{submission_id}").json()["status"]


def forward_actions(client, submission_id):
    entries = client.get(f"/submissions/{submission_id}/audit").json()
    return [e for e in entries if e["action"] == "FORWARD"]


def personal_data(submission_id):
    record = storage.get(submission_id)
    return [record[key] for key in ("personalCode", "fullName", "email", "body")]


def add_received(received_at: str) -> str:
    """Jauns RECEIVED iesniegums ar norādīto saņemšanas laiku."""
    return storage.add({**storage.SEED[0], "receivedAt": received_at})["id"]


def forward_at(client, monkeypatch, submission_id, forwarded_at: datetime):
    monkeypatch.setattr(clock, "now", lambda: forwarded_at)
    return forward(client, submission_id)


def utc(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# 1.–2. kritērijs


def test_crb_ac1_received_forwarded(seeded):
    due_before = seeded.get(f"/submissions/{RECEIVED}").json()["dueDate"]

    response = forward(seeded, RECEIVED, "VCD")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == RECEIVED
    assert body["status"] == "FORWARDED"
    assert body["forwardedTo"] == "VCD"
    assert body["forwardedAt"] == "2026-10-05T12:00:00Z"
    assert body["forwardedLate"] is False
    # Komentārs pieteikumā: dueDate pēc pārsūtīšanas nemainās.
    assert body["dueDate"] == due_before
    assert seeded.get(f"/submissions/{RECEIVED}").json() == body


def test_crb_ac2_in_progress_forwarded(seeded):
    response = forward(seeded, IN_PROGRESS, "EZM-SOC")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "FORWARDED"
    assert body["forwardedTo"] == "EZM-SOC"
    assert status_of(seeded, IN_PROGRESS) == "FORWARDED"


def test_crb_not_forwarded_has_null_forward_info(seeded):
    body = seeded.get(f"/submissions/{RECEIVED}").json()

    assert body["forwardedTo"] is None
    assert body["forwardedAt"] is None
    assert body["forwardedLate"] is None


# 3.–4. kritērijs: 5 darba dienas, saņemšanas diena ir 0. diena (variants A)


@pytest.mark.parametrize(
    ("received_at", "forwarded_at", "late"),
    [
        # Pieteikuma piemērs: saņemts pk. 25.09., pēdējā diena laikā ir pk. 02.10.
        ("2026-09-25T13:40:00+00:00", utc(2026, 10, 1), False),
        ("2026-09-25T13:40:00+00:00", utc(2026, 10, 2), False),
        ("2026-09-25T13:40:00+00:00", utc(2026, 10, 5), True),
        # 18.11. ir svētki: pēdējā diena laikā ir 24.11.
        ("2026-11-16T09:00:00+00:00", utc(2026, 11, 24), False),
        ("2026-11-16T09:00:00+00:00", utc(2026, 11, 25), True),
        # Saņemts sestdienā: pēdējā diena laikā ir pk. 02.10.
        ("2026-09-26T10:00:00+00:00", utc(2026, 10, 2), False),
        ("2026-09-26T10:00:00+00:00", utc(2026, 10, 5), True),
        # Skaita datumus, ne stundas.
        ("2026-09-25T23:59:00+00:00", utc(2026, 10, 2, 23, 59), False),
        ("2026-09-25T23:59:00+00:00", utc(2026, 10, 5, 0, 1), True),
    ],
)
def test_crb_ac3_ac4_forwarded_late(
    seeded, monkeypatch, received_at, forwarded_at, late
):
    submission_id = add_received(received_at)

    response = forward_at(seeded, monkeypatch, submission_id, forwarded_at)

    assert response.status_code == 200
    assert response.json()["forwardedLate"] is late
    assert seeded.get(f"/submissions/{submission_id}").json()["forwardedLate"] is late


def test_crb_forwarded_late_counts_utc_dates():
    # 25.09. 00:30 Rīgas laikā ir 24.09. 21:30 UTC: pēdējā diena laikā ir 01.10.
    received = datetime.fromisoformat("2026-09-25T00:30:00+03:00")

    assert is_forwarded_late(received, utc(2026, 10, 1)) is False
    assert is_forwarded_late(received, utc(2026, 10, 2)) is True


def test_crb_forwarded_late_is_stored_not_recomputed(seeded, monkeypatch):
    submission_id = add_received("2026-09-25T13:40:00+00:00")
    forward_at(seeded, monkeypatch, submission_id, utc(2026, 10, 2))

    monkeypatch.setattr(clock, "now", lambda: utc(2026, 12, 1))

    body = seeded.get(f"/submissions/{submission_id}").json()
    assert body["forwardedLate"] is False
    assert body["forwardedAt"] == "2026-10-02T12:00:00Z"


# 5. kritērijs


@pytest.mark.parametrize(
    "code",
    ["XYZ", "", "vcd", " VCD ", "XYZ' OR '1'='1", "O'Brien", "VCD'--"],
    ids=["xyz", "empty", "lowercase", "padded", "sql-or", "quote", "sql-comment"],
)
def test_crb_ac5_unknown_institution_400(seeded, code):
    response = forward(seeded, RECEIVED, code)

    assert response.status_code == 400
    assert response.json() == {
        "error": {"code": "UNKNOWN_INSTITUTION", "message": "Unknown institution"}
    }
    assert status_of(seeded, RECEIVED) == "RECEIVED"
    assert forward_actions(seeded, RECEIVED) == []


def test_crb_ac5_unknown_institution_before_state_check(seeded):
    response = forward(seeded, ANSWERED, "XYZ")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNKNOWN_INSTITUTION"


# 6. kritērijs


@pytest.mark.parametrize(
    ("payload", "issue"),
    [({}, "REQUIRED"), ({"institutionCode": None}, "INVALID_FORMAT")],
    ids=["missing", "null"],
)
def test_crb_ac6_missing_institution_code_400(seeded, payload, issue):
    response = seeded.post(f"/submissions/{RECEIVED}/forward", json=payload)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == [{"field": "institutionCode", "issue": issue}]
    assert status_of(seeded, RECEIVED) == "RECEIVED"


def test_crb_ac6_no_body_400(seeded):
    response = seeded.post(f"/submissions/{RECEIVED}/forward")

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == [{"field": "institutionCode", "issue": "REQUIRED"}]
    assert status_of(seeded, RECEIVED) == "RECEIVED"


def test_crb_ac6_not_a_string_400(seeded):
    response = seeded.post(
        f"/submissions/{RECEIVED}/forward", json={"institutionCode": 123}
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"] == [{"field": "institutionCode", "issue": "INVALID_FORMAT"}]
    assert status_of(seeded, RECEIVED) == "RECEIVED"


# 7. kritērijs


@pytest.mark.parametrize(
    ("submission_id", "status"),
    [(FORWARDED, "FORWARDED"), (ANSWERED, "ANSWERED"), (WITHDRAWN, "WITHDRAWN")],
)
def test_crb_ac7_invalid_state_409(seeded, submission_id, status):
    response = forward(seeded, submission_id, "VCD")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"
    body = seeded.get(f"/submissions/{submission_id}").json()
    assert body["status"] == status
    assert body["forwardedTo"] is None
    assert forward_actions(seeded, submission_id) == []


def test_crb_ac7_second_forward_409(seeded):
    assert forward(seeded, RECEIVED, "VCD").status_code == 200

    response = forward(seeded, RECEIVED, "EZM-BUV")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "INVALID_STATE"
    assert seeded.get(f"/submissions/{RECEIVED}").json()["forwardedTo"] == "VCD"
    assert len(forward_actions(seeded, RECEIVED)) == 1


# 8. kritērijs


def test_crb_ac8_unknown_id_404(seeded):
    response = forward(seeded, UNKNOWN)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


# 9. kritērijs


def test_crb_ac9_audit_forward_entry(seeded):
    forward(seeded, RECEIVED, "VCD")

    assert forward_actions(seeded, RECEIVED) == [
        {"at": "2026-10-05T12:00:00Z", "action": "FORWARD", "detail": "VCD"}
    ]


# 10. kritērijs


def test_crb_ac10_no_personal_data_in_log_or_errors(seeded, caplog):
    caplog.set_level(logging.DEBUG)
    secrets_ = personal_data(RECEIVED) + personal_data(ANSWERED)

    responses = [
        seeded.post(f"/submissions/{RECEIVED}/forward", json={}),
        seeded.post(f"/submissions/{RECEIVED}/forward"),
        forward(seeded, RECEIVED, "XYZ' OR '1'='1"),
        forward(seeded, ANSWERED),
        forward(seeded, UNKNOWN),
        forward(seeded, RECEIVED),
        forward(seeded, RECEIVED),
    ]
    errors = [r.text for r in responses if r.status_code >= 400]

    assert len(errors) == 6
    assert "Iesniegums pārsūtīts" in caplog.text
    for value in secrets_:
        assert value not in caplog.text
        assert all(value not in text for text in errors)


# Regresija: CR-3 saraksts


def test_crb_list_items_have_no_forward_info(seeded):
    forward(seeded, RECEIVED)

    items = seeded.get("/submissions").json()

    assert items
    assert all(not key.startswith("forwarded") for item in items for key in item)


# Glabātuve: parametrizēts vaicājums (kontrolsaraksta 7. punkts)


@pytest.mark.parametrize("code", ["XYZ' OR '1'='1", "O'Brien", "VCD'--"])
def test_crb_find_institution_input_is_not_sql(code):
    storage.reset(seed=False)

    assert storage.find_institution(code) is None


def test_crb_find_institution_known_code():
    storage.reset(seed=False)

    assert storage.find_institution("VCD")["code"] == "VCD"
