"""Ezermalas pieteikumu sistēma · iesniegumu API (mācību prototips)."""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Body, Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import clock, omd_client, storage, working_days
from app.errors import (
    InvalidState,
    SubmissionNotFound,
    UnknownInstitution,
    register_error_handlers,
)
from app.models import (
    AuditEntry,
    Error,
    ForwardRequest,
    Health,
    PreferredChannel,
    ReasonCode,
    ReplyChannel,
    Submission,
    SubmissionCreate,
    SubmissionCreated,
    SubmissionListItem,
    SubmissionStatus,
    Topic,
    TopicItem,
    WithdrawRequest,
)

VERSION = "0.1.0"
TOPIC_NAMES = {
    Topic.ROADS: "Ceļi un ielas",
    Topic.WASTE: "Atkritumi",
    Topic.PLANNING: "Teritorijas plānošana",
    Topic.PARKS: "Parki un skvēri",
    Topic.OTHER: "Cits",
}
# CR-A: FORWARDED nav atļauts (pieņēmums atvērtajam jautājumam, sk. PR piezīmi).
WITHDRAWABLE = (SubmissionStatus.RECEIVED.value, SubmissionStatus.IN_PROGRESS.value)
FORWARDABLE = (SubmissionStatus.RECEIVED.value, SubmissionStatus.IN_PROGRESS.value)
FORWARD_DAYS = 5  # CR-B: pārsūtīšanas termiņš darba dienās
REPLY_DAYS = 30  # Vienkāršots termiņš: 30 kalendāra dienas
UI_DIR = Path(__file__).resolve().parent.parent / "ui"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ezermala.submissions")

app = FastAPI(title="Ezermalas pieteikumu sistēma · iesniegumu API", version=VERSION)
register_error_handlers(app)
storage.reset()


OmdLookup = Callable[[str], omd_client.OmdCheck]


def get_omd() -> OmdLookup:
    return omd_client.mailbox_status


def decide_reply_channel(
    preferred: PreferredChannel, check: omd_client.OmdCheck
) -> tuple[ReplyChannel, ReasonCode | None]:
    """CR-2: atbildes kanāls pēc OMD atbildes. Ja reģistrs neatbild skaidri, nemin."""
    if check.result is omd_client.OmdResult.ACTIVE:
        return ReplyChannel.E_ADDRESS, None
    if check.result is omd_client.OmdResult.NOT_ACTIVATED:
        if preferred is PreferredChannel.E_ADDRESS:
            return ReplyChannel.EMAIL, ReasonCode.E_ADDRESS_NOT_ACTIVE
        return ReplyChannel(preferred.value), None
    return ReplyChannel.PENDING_CHANNEL_CHECK, ReasonCode.REGISTER_UNAVAILABLE


def is_forwarded_late(received_at: datetime, forwarded_at: datetime) -> bool:
    """CR-B: vai pārsūtīts vēlāk nekā 5 darba dienas pēc saņemšanas (UTC datumi)."""
    # Saņemšanas diena ir 0. diena (izstrādātāja lēmums, sk. PR aprakstu).
    received = received_at.astimezone(timezone.utc).date()
    deadline = working_days.add_working_days(received, FORWARD_DAYS)
    return forwarded_at.astimezone(timezone.utc).date() > deadline


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/ui/")


@app.get("/health", response_model=Health, tags=["Sistēma"])
def get_health() -> Health:
    return Health(status="ok", version=VERSION)


@app.get("/topics", response_model=list[TopicItem], tags=["Klasifikatori"])
def list_topics() -> list[TopicItem]:
    return [TopicItem(code=code, name=name) for code, name in TOPIC_NAMES.items()]


@app.post(
    "/submissions",
    status_code=201,
    response_model=SubmissionCreated,
    responses={400: {"model": Error}},
    tags=["Iesniegumi"],
)
def create_submission(
    data: SubmissionCreate,
    omd: Annotated[OmdLookup, Depends(get_omd)],
) -> SubmissionCreated:
    received_at = clock.now()
    check = omd(data.personalCode)
    reply_channel, reason = decide_reply_channel(data.preferredChannel, check)

    record = storage.add(
        {
            **data.model_dump(mode="json"),
            "status": SubmissionStatus.RECEIVED.value,
            "receivedAt": received_at.isoformat(),
            "dueDate": (received_at.date() + timedelta(days=REPLY_DAYS)).isoformat(),
            "replyChannel": reply_channel.value,
            "reasonCode": reason.value if reason else None,
        }
    )
    storage.add_audit(record["id"], "CREATE")
    # Žurnālā tikai iesnieguma ID un iemesls. Nekad personas kods vai teksts.
    logger.info("Iesniegums saņemts: %s", record["id"])
    if check.result is omd_client.OmdResult.UNAVAILABLE:
        logger.warning(
            "OMD pārbaude neizdevās: iesniegums %s, iemesls %s (%s)",
            record["id"],
            ReasonCode.REGISTER_UNAVAILABLE.value,
            check.detail,
        )
    return SubmissionCreated(**record)


@app.get(
    "/submissions",
    response_model=list[SubmissionListItem],
    tags=["Iesniegumi"],
)
def list_submissions(
    status: SubmissionStatus | None = None, topic: Topic | None = None
) -> list[SubmissionListItem]:
    records = storage.list_submissions(
        status=status.value if status else None,
        topic=topic.value if topic else None,
    )
    return [SubmissionListItem(**record) for record in records]


@app.get(
    "/submissions/{submission_id}",
    response_model=Submission,
    responses={404: {"model": Error}},
    tags=["Iesniegumi"],
)
def get_submission(submission_id: str) -> Submission:
    record = storage.get(submission_id)
    if record is None:
        raise SubmissionNotFound()
    return Submission(**record)


@app.get(
    "/submissions/{submission_id}/audit",
    response_model=list[AuditEntry],
    responses={404: {"model": Error}},
    tags=["Iesniegumi"],
)
def get_submission_audit(submission_id: str) -> list[AuditEntry]:
    if storage.get(submission_id) is None:
        raise SubmissionNotFound()
    return [AuditEntry(**entry) for entry in storage.list_audit(submission_id)]


@app.post(
    "/submissions/{submission_id}/withdraw",
    response_model=Submission,
    responses={400: {"model": Error}, 404: {"model": Error}, 409: {"model": Error}},
    tags=["Darbības ar iesniegumu"],
)
def withdraw_submission(
    submission_id: str,
    data: Annotated[WithdrawRequest | None, Body()] = None,
) -> Submission:
    if storage.get(submission_id) is None:
        raise SubmissionNotFound()
    if data is None:
        # Pieprasījumā nav ķermeņa: tāpat kā tad, ja trūkst iemesla.
        raise RequestValidationError(
            [{"type": "missing", "loc": ("body", "reason"), "msg": "Field required"}]
        )
    record = storage.update_status(
        submission_id, SubmissionStatus.WITHDRAWN.value, allowed_from=WITHDRAWABLE
    )
    if record is None:
        raise InvalidState()
    storage.add_audit(submission_id, "WITHDRAW", data.reason)
    # Iemeslu žurnālā neraksta: brīvā tekstā var būt personas dati.
    logger.info("Iesniegums atsaukts: %s", submission_id)
    return Submission(**record)


@app.post(
    "/submissions/{submission_id}/forward",
    response_model=Submission,
    responses={400: {"model": Error}, 404: {"model": Error}, 409: {"model": Error}},
    tags=["Darbības ar iesniegumu"],
)
def forward_submission(
    submission_id: str,
    data: Annotated[ForwardRequest | None, Body()] = None,
) -> Submission:
    record = storage.get(submission_id)
    if record is None:
        raise SubmissionNotFound()
    if data is None:
        # Pieprasījumā nav ķermeņa: tāpat kā tad, ja trūkst iestādes koda.
        raise RequestValidationError(
            [
                {
                    "type": "missing",
                    "loc": ("body", "institutionCode"),
                    "msg": "Field required",
                }
            ]
        )
    if storage.find_institution(data.institutionCode) is None:
        raise UnknownInstitution()
    forwarded_at = clock.now()
    late = is_forwarded_late(datetime.fromisoformat(record["receivedAt"]), forwarded_at)
    record = storage.forward(
        submission_id,
        data.institutionCode,
        forwarded_at.isoformat(),
        late,
        allowed_from=FORWARDABLE,
    )
    if record is None:
        raise InvalidState()
    storage.add_audit(submission_id, "FORWARD", data.institutionCode)
    # Iestādes kods ir no slēgta saraksta, tas nav personas dati.
    logger.info("Iesniegums pārsūtīts: %s -> %s", submission_id, data.institutionCode)
    return Submission(**record)


app.mount("/ui", StaticFiles(directory=UI_DIR, html=True), name="ui")
