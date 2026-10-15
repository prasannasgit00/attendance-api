"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real environment variables win)

All business rules (R1-R10) are described in openapi.yaml; the stored shapes in DATA_MODEL.md.
Instants are BSON datetimes in MongoDB (UTC) and epoch milliseconds in the HTTP API.
"""
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import Decimal128, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does not override variables that are already set in the environment

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,  # datetimes come back as aware UTC, never naive
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
ALL_STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_HOURS = Decimal("4.50")
MAX_SHIFT_SECONDS = 24 * 3600
MAX_TREND_DAYS = 92

MIN_MS = 100_000_000_000
MAX_MS = 4_102_444_800_000
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]

MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
TIME_PATTERN = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"


# --------------------------------------------------------------------------- #
# Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
INDEXES = [
    # emp_code is the business key: uniqueness is enforced by the database, never by check-then-insert.
    ("employees", [("emp_code", 1)], {"unique": True, "name": "emp_code_unique"}),
    # filtered employee list (sorted by emp_code), department lookups for the trend.
    ("employees", [("department", 1), ("emp_code", 1)], {"name": "department_emp_code"}),
    # department summary headcount (joined_on <= end of month) when no department filter is given.
    ("employees", [("joined_on", 1)], {"name": "joined_on"}),
    # one record per employee per attendance day; per-employee queries; the join key for analytics.
    ("attendance_logs", [("emp_code", 1), ("date", 1)], {"unique": True, "name": "emp_code_date_unique"}),
    # GET /attendance sort (date desc, emp_code asc) and month/date-range scans across all employees.
    ("attendance_logs", [("date", -1), ("emp_code", 1)], {"name": "date_desc_emp_code"}),
]
_indexes_ready = False


def ensure_indexes() -> bool:
    global _indexes_ready
    ok = True
    for coll, keys, opts in INDEXES:
        try:
            db[coll].create_index(keys, **opts)
        except PyMongoError as exc:  # keep the app up; /health will retry on the next probe
            ok = False
            print(f"index creation failed on {coll} {opts.get('name')}: {exc}")
    _indexes_ready = ok
    return ok


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except Exception as exc:  # database may not be reachable yet
        print(f"startup index creation skipped: {exc}")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
def mongo_error_handler(_request, exc):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# Small helpers: errors, time, dates, rounding
# --------------------------------------------------------------------------- #
def unprocessable(loc, msg):
    raise HTTPException(
        status_code=422,
        detail=[{"type": "value_error", "loc": list(loc), "msg": msg, "input": None}],
    )


def parse_date(value: str, loc) -> date:
    if not DATE_RE.match(value):
        unprocessable(loc, "date must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        unprocessable(loc, "not a valid calendar date")


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    """BSON datetime -> epoch milliseconds, with integer arithmetic only."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt - EPOCH
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def from_ms(ms: int) -> datetime:
    return EPOCH + timedelta(milliseconds=ms)


def instant(ms: Optional[int]) -> datetime:
    """R1: instants are truncated to whole seconds. Defaults to the current time."""
    seconds = int(time.time()) if ms is None else ms // 1000
    return EPOCH + timedelta(seconds=seconds)


def round_half_up(value: Decimal, places: int) -> float:
    return float(value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def dec_to_float(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    return float(value)


def month_bounds(month: str):
    year, mon = int(month[:4]), int(month[5:7])
    try:
        start = date(year, mon, 1)
        nxt = date(year + 1, 1, 1) if mon == 12 else date(year, mon + 1, 1)
    except ValueError:
        unprocessable(("query", "month"), "invalid month")
    return start, nxt - timedelta(days=1)


def count_weekdays(start: date, end: date) -> int:
    n, d = 0, start
    while d <= end:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


# --------------------------------------------------------------------------- #
# Business rules R1-R5
# --------------------------------------------------------------------------- #
def _hm(value: str):
    h, m = value.split(":")
    return int(h), int(m)


def is_overnight(emp: dict) -> bool:
    return _hm(emp["shift_end"]) <= _hm(emp["shift_start"])


def attendance_date(punch_in: datetime, emp: dict) -> date:
    """R1: IST date of the punch-in; early-morning punches of an overnight shift belong to the previous day."""
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if is_overnight(emp):
        eh, em = _hm(emp["shift_end"])
        if (ist.hour, ist.minute, ist.second) < (eh, em, 0):
            d -= timedelta(days=1)
    return d


def shift_start_dt(d: date, emp: dict) -> datetime:
    h, m = _hm(emp["shift_start"])
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def shift_end_dt(d: date, emp: dict) -> datetime:
    h, m = _hm(emp["shift_end"])
    end = datetime(d.year, d.month, d.day, h, m, tzinfo=IST)
    return end + timedelta(days=1) if is_overnight(emp) else end


def _seconds(delta: timedelta) -> int:
    return delta.days * 86400 + delta.seconds


def compute_late_minutes(punch_in: datetime, shift_start: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then whole minutes since shift start."""
    secs = _seconds(punch_in - shift_start)
    return secs // 60 if secs > GRACE_SECONDS else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    """R4: seconds / 3600, 2 decimals, half-up."""
    hours = Decimal(_seconds(punch_out - punch_in)) / Decimal(3600)
    return hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_overtime(punch_out: datetime, shift_end: datetime) -> int:
    """R3: whole minutes after shift end, only when >= 30."""
    minutes = _seconds(punch_out - shift_end) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def derive(emp: dict, date_str: str, status: str, pin: Optional[datetime], pout: Optional[datetime]) -> dict:
    """The four stored derived fields for a record, per the table in DATA_MODEL.md."""
    zero = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or pin is None:
        return zero
    d = date.fromisoformat(date_str)
    late = compute_late_minutes(pin, shift_start_dt(d, emp))
    if pout is None:
        return {**zero, "late_minutes": late}
    wh = compute_work_hours(pin, pout)
    return {
        "work_hours": float(wh),
        "late_minutes": late,
        "overtime_minutes": compute_overtime(pout, shift_end_dt(d, emp)),
        "half_day": wh < HALF_DAY_HOURS,
    }


# --------------------------------------------------------------------------- #
# Serialisation (BSON -> API)
# --------------------------------------------------------------------------- #
def ser_employee(d: dict) -> dict:
    return {
        "emp_code": d["emp_code"],
        "name": d["name"],
        "email": d["email"],
        "department": d["department"],
        "shift_start": d["shift_start"],
        "shift_end": d["shift_end"],
        "joined_on": d["joined_on"],
        "created_at": to_ms(d.get("created_at")),
    }


def _ser_change_value(v):
    return to_ms(v) if isinstance(v, datetime) else v


def ser_record(d: dict) -> dict:
    history = []
    for h in d.get("history") or []:
        history.append(
            {
                "at": to_ms(h["at"]),
                "by": h["by"],
                "reason": h["reason"],
                "changes": {
                    k: {"from": _ser_change_value(v.get("from")), "to": _ser_change_value(v.get("to"))}
                    for k, v in (h.get("changes") or {}).items()
                },
            }
        )
    wh = d.get("work_hours")
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": to_ms(d.get("punch_in")),
        "punch_out": to_ms(d.get("punch_out")),
        "work_hours": None if wh is None else float(wh),
        "late_minutes": d.get("late_minutes") or 0,
        "overtime_minutes": d.get("overtime_minutes") or 0,
        "half_day": bool(d.get("half_day", False)),
        "history": history,
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=TIME_PATTERN)
    shift_end: str = Field(default="18:30", pattern=TIME_PATTERN)
    joined_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @field_validator("joined_on")
    @classmethod
    def _valid_date(cls, v):
        date.fromisoformat(v)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    if not _indexes_ready:
        ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    created = from_ms(int(time.time() * 1000))
    doc = body.model_dump()
    doc["created_at"] = created
    try:
        db.employees.insert_one(doc)  # the unique index on emp_code decides the winner
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return ser_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    cur = db.employees.find(q).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [ser_employee(d) for d in cur], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = get_employee_or_404(body.emp_code)
    pin = instant(body.punched_at)
    att_date = attendance_date(pin, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": pin,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(pin, shift_start_dt(att_date, emp)),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent request wins
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return ser_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = get_employee_or_404(body.emp_code)
    t = instant(body.punched_at)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": t}}, sort=[("punch_in", -1)]
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    secs = _seconds(t - rec["punch_in"])
    if secs <= 0:
        unprocessable(("body", "punched_at"), "punched_at must be after punch_in")
    if secs > MAX_SHIFT_SECONDS:
        unprocessable(("body", "punched_at"), "punched_at is more than 24 hours after punch_in")

    d = date.fromisoformat(rec["date"])
    wh = compute_work_hours(rec["punch_in"], t)
    update = {
        "punch_out": t,
        "work_hours": float(wh),
        "overtime_minutes": compute_overtime(t, shift_end_dt(d, emp)),
        "half_day": wh < HALF_DAY_HOURS,
    }
    # Conditional update: only one of several simultaneous punch-outs can match punch_out == null.
    res = db.attendance_logs.update_one({"_id": rec["_id"], "punch_out": None}, {"$set": update})
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    return ser_record(db.attendance_logs.find_one({"_id": rec["_id"]}))


def build_attendance_query(emp_code, date_from, date_to, status, page, page_size) -> dict:
    d_from = parse_date(date_from, ("query", "date_from")) if date_from is not None else None
    d_to = parse_date(date_to, ("query", "date_to")) if date_to is not None else None
    if d_from and d_to and d_from > d_to:
        unprocessable(("query", "date_from"), "date_from must not be after date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = date_from
        if d_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return {
        "filter": q,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
    coll = db.attendance_logs
    total = coll.count_documents(spec["filter"]) if spec["filter"] else coll.estimated_document_count()
    cur = coll.find(
        spec["filter"], sort=list(spec["sort"].items()), skip=spec["skip"], limit=spec["limit"]
    )
    return {"items": [ser_record(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, body: RegularizeRequest, date_str: str = Path(..., alias="date")):
    parse_date(date_str, ("path", "date"))
    emp = get_employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_str})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    old_status = rec["status"]
    old_pin, old_pout = rec.get("punch_in"), rec.get("punch_out")
    new_status = body.status or old_status
    sup_in = instant(body.punch_in) if body.punch_in is not None else None
    sup_out = instant(body.punch_out) if body.punch_out is not None else None

    if new_status in ("ABSENT", "LEAVE"):
        if sup_in is not None or sup_out is not None:
            unprocessable(("body", "status"), f"{new_status} cannot carry punch times")
        new_pin = new_pout = None
    else:
        new_pin = sup_in if sup_in is not None else old_pin
        new_pout = sup_out if sup_out is not None else old_pout
        if new_pin is None:
            unprocessable(("body", "punch_in"), "a presence status requires a punch_in")
        if sup_in is not None and attendance_date(sup_in, emp).isoformat() != date_str:
            unprocessable(("body", "punch_in"), "punch_in must fall on the record's attendance date")
        if new_pout is not None:
            gap = _seconds(new_pout - new_pin)
            if gap <= 0:
                unprocessable(("body", "punch_out"), "punch_out must be after punch_in")
            if gap > MAX_SHIFT_SECONDS:
                unprocessable(("body", "punch_out"), "punch_out must be within 24 hours of punch_in")

    if new_status == old_status and new_pin == old_pin and new_pout == old_pout:
        unprocessable(("body",), "the request changes nothing")

    new_vals = {"status": new_status, "punch_in": new_pin, "punch_out": new_pout}
    new_vals.update(derive(emp, date_str, new_status, new_pin, new_pout))
    old_vals = {
        "status": old_status,
        "punch_in": old_pin,
        "punch_out": old_pout,
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }
    changes = {
        k: {"from": old_vals[k], "to": new_vals[k]} for k in new_vals if new_vals[k] != old_vals[k]
    }
    at = from_ms(int(time.time() * 1000))
    entry = {"at": at, "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: the write only applies if the record is exactly as we read it
    # (same fields, same history length), so concurrent edits cannot silently lose history.
    if "history" in rec and rec["history"] is not None:
        hist_cond = {"$size": len(rec["history"])}
    else:
        hist_cond = {"$exists": False}
    cond = {
        "_id": rec["_id"],
        "status": old_status,
        "punch_in": old_pin,
        "punch_out": old_pout,
        "history": hist_cond,
    }
    res = db.attendance_logs.update_one(cond, {"$set": new_vals, "$push": {"history": entry}})
    if res.matched_count == 0:
        raise HTTPException(409, "record was modified concurrently, please retry")
    return ser_record(db.attendance_logs.find_one({"_id": rec["_id"]}))


# --------------------------------------------------------------------------- #
# Analytics - pipeline builders (shared by the endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def _lm():
    return {"$ifNull": ["$late_minutes", 0]}


def _is_present():
    return {"$in": ["$status", PRESENCE]}


def _half_flag():
    return {"$eq": [{"$ifNull": ["$half_day", False]}, True]}


def _present_halves(weekends=None):
    """Half-day units: 2 for a full present day, 1 for a half day, 0 otherwise (optionally Mon-Fri only)."""
    cond = _is_present()
    if weekends is not None:
        cond = {"$and": [cond, {"$not": [{"$in": ["$date", weekends]}]}]}
    return {"$cond": [cond, {"$cond": [_half_flag(), 1, 2]}, 0]}


def _weekend_dates(start: date, end: date):
    out, d = [], start
    while d <= end:
        if d.weekday() >= 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def p_monthly(emp_code: str, month: str):
    start, end = month_bounds(month)
    weekends = _weekend_dates(start, end)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "halves": {"$sum": _present_halves(weekends)},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [_lm(), 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": _lm()},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def p_summary(month: str, department: Optional[str]):
    start, end = month_bounds(month)
    weekends = _weekend_dates(start, end)
    emp_match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        emp_match["department"] = department
    has_wh = {"$and": [_is_present(), {"$isNumber": "$work_hours"}]}
    per_employee = [
        {"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "halves": {"$sum": _present_halves(weekends)},
                # work_hours in hundredths (stored values have 2 decimals) so sums and the mean are exact
                "wh_cents": {
                    "$sum": {
                        "$cond": [has_wh, {"$toLong": {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}}, 0]
                    }
                },
                "wh_n": {"$sum": {"$cond": [has_wh, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [_lm(), 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": _lm()},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
    ]
    fields = ["halves", "wh_cents", "wh_n", "late_count", "total_late_minutes", "leave_count", "on_duty_count"]
    group = {"_id": "$department", "headcount": {"$sum": 1}}
    for f in fields:
        group[f] = {"$sum": {"$ifNull": [f"$a.{f}", 0]}}
    return [
        {"$match": emp_match},  # every employee who has joined by month end, with or without logs
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": per_employee,
                "as": "agg",
            }
        },
        {"$addFields": {"a": {"$arrayElemAt": ["$agg", 0]}}},
        {"$group": group},
        {"$sort": {"_id": 1}},
    ]


def p_leaderboard(month: str, limit: int, department: Optional[str]):
    start, end = month_bounds(month)
    pipeline = [
        {
            "$match": {
                "date": {"$gte": start.isoformat(), "$lte": end.isoformat()},
                "late_minutes": {"$gt": 0},
            }
        },
        {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees drop out here
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})
    pipeline += [
        # $rank = standard competition ranking (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applies after ranking, so ties are never cut
        {"$sort": {"total": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": "$total",
                "late_count": 1,
            }
        },
    ]
    return pipeline


def _half_up_expr(expr, places: int):
    """Exact half-up rounding of a Decimal128 expression (MongoDB's $round is half-to-even)."""
    scale = 10**places
    return {
        "$divide": [
            {"$floor": {"$add": [{"$multiply": [expr, scale]}, Decimal128("0.5")]}},
            Decimal128(str(scale)),
        ]
    }


def p_trend(emps: list, d_from: date, d_to: date):
    codes = [e["emp_code"] for e in emps]
    joined = [e["joined_on"] for e in emps]
    lo = datetime(d_from.year, d_from.month, d_from.day, tzinfo=UTC)
    hi = datetime(d_to.year, d_to.month, d_to.day, tzinfo=UTC)
    rate = {
        "$divide": [{"$toDecimal": "$halves"}, {"$toDecimal": {"$multiply": [2, "$headcount"]}}]
    }
    return [
        {
            "$match": {
                "emp_code": {"$in": codes},
                "date": {"$gte": d_from.isoformat(), "$lte": d_to.isoformat()},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "halves": {"$sum": _present_halves()},
                "late_count": {"$sum": {"$cond": [{"$gt": [_lm(), 0]}, 1, 0]}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}},
                "halves": 1,
                "late_count": 1,
            }
        },
        # one out-of-range sentinel row so $densify always has input, even for a range with no records
        {
            "$unionWith": {
                "pipeline": [
                    {"$documents": [{"d": lo - timedelta(days=1), "halves": 0, "late_count": 0}]}
                ]
            }
        },
        {"$sort": {"d": 1}},
        {
            "$densify": {
                "field": "d",
                "range": {"step": 1, "unit": "day", "bounds": [lo, hi + timedelta(days=1)]},
            }
        },
        {"$match": {"d": {"$gte": lo, "$lte": hi}}},
        {"$sort": {"d": 1}},
        {
            "$addFields": {
                "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
                "halves": {"$ifNull": ["$halves", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": {"$literal": joined},
                            "cond": {"$lte": ["$$this", "$date"]},
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        _half_up_expr(rate, 4),
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {"mavg": {"$avg": "$rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "halves": 1,
                "late_count": 1,
                "rate": 1,
                "moving": _half_up_expr("$mavg", 4),
            }
        },
    ]


# --------------------------------------------------------------------------- #
# Analytics - endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_PATTERN)):
    emp = get_employee_or_404(emp_code)
    start, end = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(p_monthly(emp_code, month)))
    r = rows[0] if rows else {}
    halves = r.get("halves", 0)

    joined = date.fromisoformat(emp["joined_on"])
    working_days = count_weekdays(max(start, joined), end) if joined <= end else 0
    pct = None
    if working_days > 0:
        pct = round_half_up(Decimal(halves) * 50 / Decimal(working_days), 2)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": halves / 2,
        "leave_days": r.get("leave_days", 0),
        "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("total_late_minutes", 0),
        "total_overtime_minutes": r.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_PATTERN), department: Optional[str] = None):
    items = []
    for r in db.employees.aggregate(p_summary(month, department)):
        n = r["wh_n"]
        avg = None
        if n > 0:
            avg = round_half_up(Decimal(r["wh_cents"]) / (Decimal(100) * n), 2)
        items.append(
            {
                "department": r["_id"],
                "headcount": r["headcount"],
                "present_days": r["halves"] / 2,
                "avg_work_hours": avg,
                "late_count": r["late_count"],
                "total_late_minutes": r["total_late_minutes"],
                "leave_count": r["leave_count"],
                "on_duty_count": r["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_PATTERN),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    items = list(db.attendance_logs.aggregate(p_leaderboard(month, limit, department)))
    return {"month": month, "items": items}


def prepare_trend(department: str, from_: str, to: str):
    d_from = parse_date(from_, ("query", "from"))
    d_to = parse_date(to, ("query", "to"))
    if d_to < d_from:
        unprocessable(("query", "to"), "to must not be before from")
    if (d_to - d_from).days + 1 > MAX_TREND_DAYS:
        unprocessable(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days")
    emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    return p_trend(emps, d_from, d_to)


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
):
    pipeline = prepare_trend(department, from_, to)
    items = []
    for r in db.attendance_logs.aggregate(pipeline):
        items.append(
            {
                "date": r["date"],
                "is_working_day": r["is_working_day"],
                "headcount": r["headcount"],
                "present_count": r["halves"] / 2,
                "late_count": r["late_count"],
                "attendance_rate": dec_to_float(r.get("rate")),
                "moving_avg_7d": dec_to_float(r.get("moving")),
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def _require(name: str, value):
    if value is None:
        unprocessable(("query", name), f"{name} is required for this endpoint")
    return value


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal[
        "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_PATTERN),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        spec = build_attendance_query(emp_code, date_from, date_to, status, page, page_size)
        collection, command = "attendance_logs", {"find": "attendance_logs", **spec}
    else:
        if endpoint == "employee_monthly":
            collection = "attendance_logs"
            pipeline = p_monthly(_require("emp_code", emp_code), _require("month", month))
        elif endpoint == "department_summary":
            collection = "employees"
            pipeline = p_summary(_require("month", month), department)
        elif endpoint == "late_leaderboard":
            collection = "attendance_logs"
            pipeline = p_leaderboard(_require("month", month), limit, department)
        else:  # department_trend
            collection = "attendance_logs"
            pipeline = prepare_trend(
                _require("department", department), _require("from", from_), _require("to", to)
            )
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS)),
    }
