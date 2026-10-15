"""
Employee Attendance & Analytics API
Run from repository root: python -m uvicorn app.main:app --reload
Environment: MONGO_URI, MONGO_DB
"""
import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import ObjectId
from bson.json_util import dumps

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=1500)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
MAX_EPOCH_MS = 4102444800000
MIN_EPOCH_MS = 100000000000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ------------------------------ startup / indexes ----------------------------

@app.on_event("startup")
def create_indexes():
    # Idempotent. Employee codes and natural attendance keys must be unique.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True, name="uniq_attendance_employee_date"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="attendance_date_emp"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING), ("status", ASCENDING)],
        name="attendance_emp_date_status"
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING), ("late_minutes", DESCENDING)],
        name="attendance_month_analytics"
    )


# --------------------------------- helpers -----------------------------------

def utc_now_seconds() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def truncate_instant(dt: datetime) -> datetime:
    return as_utc(dt).replace(microsecond=0)


def epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return int(truncate_instant(dt).timestamp() * 1000)


def parse_epoch_ms(value: Optional[int]) -> datetime:
    if value is None:
        return utc_now_seconds()
    # Pydantic normally rejects floats/strings, but keep boundary validation explicit.
    if type(value) is not int or value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be epoch milliseconds in the supported range")
    try:
        return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        raise HTTPException(422, "invalid timestamp")


def local_dt(dt: datetime) -> datetime:
    return as_utc(dt).astimezone(IST).replace(microsecond=0)


def date_str_valid(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise HTTPException(422, "date must be YYYY-MM-DD")


def month_bounds(month: str) -> tuple[str, str]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, mon = map(int, month.split("-"))
    last = calendar.monthrange(year, mon)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def rounded(value: Optional[float], places: int = 2) -> Optional[float]:
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def compute_late_minutes(
    punch_in: datetime, shift_start: str, shift_end: Optional[str] = None
) -> int:
    local = local_dt(punch_in)
    h, m = map(int, shift_start.split(":"))
    start_minutes = h * 60 + m
    current_minutes = local.hour * 60 + local.minute
    shift_start_dt = local.replace(hour=h, minute=m, second=0, microsecond=0)

    # For overnight shifts, an early-morning punch before shift_end belongs
    # to the shift that started on the previous calendar day.
    if shift_end is not None:
        end_h, end_m = map(int, shift_end.split(":"))
        if (end_h, end_m) <= (h, m) and current_minutes < end_h * 60 + end_m:
            shift_start_dt -= timedelta(days=1)
    # For a daytime shift, arriving before shift start is not late.
    elif current_minutes < start_minutes:
        return 0

    delta = (local - shift_start_dt).total_seconds()
    return int(delta // 60) if delta > 600 else 0


def shift_end_instant(punch_in: datetime, shift_start: str, shift_end: str) -> datetime:
    local_in = local_dt(punch_in)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    start_mins, end_mins = sh * 60 + sm, eh * 60 + em
    shift_date = local_in.date()
    if end_mins <= start_mins and local_in.hour * 60 + local_in.minute < end_mins:
        shift_date -= timedelta(days=1)
    end_date = shift_date + (timedelta(days=1) if end_mins <= start_mins else timedelta())
    return datetime.combine(end_date, time(eh, em), tzinfo=IST).astimezone(UTC).replace(microsecond=0)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = Decimal(str((truncate_instant(punch_out) - truncate_instant(punch_in)).total_seconds()))
    hours = seconds / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, punch_in: datetime, shift_start: str, shift_end: str) -> int:
    end = shift_end_instant(punch_in, shift_start, shift_end)
    mins = int((truncate_instant(punch_out) - end).total_seconds() // 60)
    return mins if mins >= 30 else 0


def attendance_date_for(punch_in: datetime, emp: dict) -> str:
    local = local_dt(punch_in)
    sh_end = tuple(map(int, emp["shift_end"].split(":")))
    sh_start = tuple(map(int, emp["shift_start"].split(":")))
    if sh_end <= sh_start and (local.hour, local.minute) < sh_end:
        local -= timedelta(days=1)
    return local.date().isoformat()


def public_record(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    out["punch_in"] = epoch_ms(out.get("punch_in"))
    out["punch_out"] = epoch_ms(out.get("punch_out"))
    out["history"] = out.get("history") or []
    out["half_day"] = bool(out.get("half_day", False))
    out["late_minutes"] = int(out.get("late_minutes", 0) or 0)
    out["overtime_minutes"] = int(out.get("overtime_minutes", 0) or 0)
    for item in out["history"]:
        item["at"] = epoch_ms(item.get("at"))
        for change in item.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = epoch_ms(change[side])
    return out


def public_employee(doc: dict) -> dict:
    out = {k: v for k, v in doc.items() if k != "_id"}
    if isinstance(out.get("created_at"), datetime):
        out["created_at"] = epoch_ms(out["created_at"])
    return out


def workdays(start: date, end: date) -> int:
    total = 0
    current = start
    while current <= end:
        if current.weekday() < 5:
            total += 1
        current += timedelta(days=1)
    return total


def validate_pagination(page: int, page_size: int):
    if page < 1:
        raise HTTPException(422, "page must be at least 1")
    if not 1 <= page_size <= 100:
        raise HTTPException(422, "page_size must be between 1 and 100")


def month_date_query(month: str) -> dict:
    first, last = month_bounds(month)
    return {"date": {"$gte": first, "$lte": last}}


def ensure_month(month: str):
    month_bounds(month)


# ---------------------------------- models -----------------------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("emp_code")
    @classmethod
    def valid_code(cls, value):
        if not re.fullmatch(r"EMP\d{4,6}", value):
            raise ValueError("emp_code must be EMP followed by 4-6 digits")
        return value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("invalid email")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def valid_time(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("time must be HH:MM")
        return value

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def different_shifts(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and (type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS):
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and (type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS):
            raise ValueError("punched_at must be epoch milliseconds")
        return value


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    status: Optional[Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, value):
        if value is not None and (type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS):
            raise ValueError("timestamps must be epoch milliseconds")
        return value


# ---------------------------------- endpoints --------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = utc_now_seconds()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    except PyMongoError:
        raise HTTPException(503, "Database unavailable")
    return public_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = [public_employee(x) for x in db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = parse_epoch_ms(body.punched_at)
    attendance_date = attendance_date_for(ts, emp)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return public_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = parse_epoch_ms(body.punched_at)
    # Search most recent open record with a punch-in not later than punch-out.
    doc = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}, "punch_out": None},
        sort=[("punch_in", DESCENDING)]
    )
    if not doc:
        any_record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
        if any_record and any_record.get("punch_out") is not None:
            raise HTTPException(409, "record already punched out")
        raise HTTPException(404, "no punch-in found")
    start = truncate_instant(doc["punch_in"])
    if ts <= start or ts - start > timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    hours = compute_work_hours(start, ts)
    overtime = compute_overtime(ts, start, emp["shift_start"], emp["shift_end"])
    half_day = hours < 4.50
    result = db.attendance_logs.update_one(
        {"_id": doc["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": half_day}}
    )
    if result.modified_count != 1:
        raise HTTPException(409, "record already punched out")
    doc.update({"punch_out": ts, "work_hours": hours, "overtime_minutes": overtime, "half_day": half_day})
    return public_record(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if status is not None and status not in STATUSES:
        raise HTTPException(422, "invalid status")
    if date_from:
        date_str_valid(date_from)
    if date_to:
        date_str_valid(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    return {"items": [public_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{attendance_date}")
def regularize_attendance(emp_code: str, attendance_date: str, body: RegularizeIn):
    date_str_valid(attendance_date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": attendance_date})
    if not old:
        raise HTTPException(404, "attendance record not found")

    current = dict(old)
    status = body.status if body.status is not None else current["status"]
    pin = parse_epoch_ms(body.punch_in) if body.punch_in is not None else current.get("punch_in")
    pout = parse_epoch_ms(body.punch_out) if body.punch_out is not None else current.get("punch_out")

    if status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        pin = pout = None
    elif pin is None:
        raise HTTPException(422, "presence status requires punch_in")

    if pin is not None:
        if attendance_date_for(pin, emp) != attendance_date:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        pin = truncate_instant(pin)
    if pout is not None:
        pout = truncate_instant(pout)
        if pin is None or pout <= pin or pout - pin > timedelta(hours=24):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    new = dict(current)
    new.update({"status": status, "punch_in": pin, "punch_out": pout})
    if status in {"ABSENT", "LEAVE"}:
        new.update({"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False})
    else:
        new["late_minutes"] = compute_late_minutes(pin, emp["shift_start"], emp["shift_end"])
        if pout is None:
            new.update({"work_hours": None, "overtime_minutes": 0, "half_day": False})
        else:
            new["work_hours"] = compute_work_hours(pin, pout)
            new["overtime_minutes"] = compute_overtime(pout, pin, emp["shift_start"], emp["shift_end"])
            new["half_day"] = new["work_hours"] < 4.50

    fields = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes = {}
    for field in fields:
        before = current.get(field, False if field == "half_day" else 0 if field in ("late_minutes", "overtime_minutes") else None)
        after = new.get(field, False if field == "half_day" else 0 if field in ("late_minutes", "overtime_minutes") else None)
        if before != after:
            changes[field] = {"from": before, "to": after}
    if not changes:
        raise HTTPException(422, "request does not change the record")

    entry = {"at": utc_now_seconds(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Atomic compare-and-set prevents silently overwriting concurrent edits/history.
    old_history = current.get("history", [])
    update_filter = {"_id": old["_id"]}
    for field in fields:
        update_filter[field] = current.get(field, None)
    update_filter["history"] = old_history
    result = db.attendance_logs.update_one(
        update_filter,
        {"$set": {k: new[k] for k in fields}, "$push": {"history": entry}}
    )
    if result.modified_count != 1:
        raise HTTPException(409, "record changed concurrently; retry")
    refreshed = db.attendance_logs.find_one({"_id": old["_id"]})
    return public_record(refreshed)


# ------------------------------- analytics ----------------------------------

def month_pipeline_match(month: str) -> dict:
    first, last = month_bounds(month)
    return {"date": {"$gte": first, "$lte": last}}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    ensure_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    first_s, last_s = month_bounds(month)
    first = date.fromisoformat(first_s)
    last = date.fromisoformat(last_s)
    effective_start = max(first, date.fromisoformat(emp["joined_on"]))
    working_days = workdays(effective_start, last) if effective_start <= last else 0

    pipeline = [
        {"$match": {"emp_code": emp_code, **month_pipeline_match(month)}},
        {"$set": {
            "_present": {"$cond": [
                {"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$lt": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 7]}]},
                {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1.0]}, 0
            ]},
            "_weekday": {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}
        }},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", list(PRESENCE)]}, {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 7]]}]}]}, {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}, 0]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}
        }}
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    data = rows[0] if rows else {}
    pct = rounded((data.get("present_days", 0) / working_days * 100), 2) if working_days else None
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": rounded(data.get("present_days", 0), 2),
        "leave_days": data.get("leave_days", 0), "late_count": data.get("late_count", 0),
        "total_late_minutes": data.get("total_late_minutes", 0),
        "total_overtime_minutes": data.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    ensure_month(month)
    first_s, last_s = month_bounds(month)
    # Start from employees so employees with no attendance logs still count toward headcount.
    pipeline = [
        {"$match": {"joined_on": {"$lte": last_s}, **({"department": department} if department is not None else {})}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]},
                {"$gte": ["$date", first_s]}, {"$lte": ["$date", last_s]}
            ]}}},
            {"$project": {"status": 1, "work_hours": 1, "late_minutes": 1, "overtime_minutes": 1, "half_day": 1, "date": 1}}
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$set": {"_present_days": {"$cond": [
            {"$and": [
                {"$in": ["$logs.status", list(PRESENCE)]},
                {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}}, [1, 7]]}]}
            ]},
            {"$cond": [{"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}, 0.5, 1]}, 0
        ]}}},
        {"$group": {"_id": "$department",
            "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": "$_present_days"},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]},
                "$logs.work_hours", 0
            ]}},
            "work_hours_count": {"$sum": {"$cond": [
                {"$and": [{"$in": ["$logs.status", list(PRESENCE)]}, {"$ne": [{"$ifNull": ["$logs.work_hours", None]}, None]}]}, 1, 0
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}}
        }},
        {"$set": {"headcount": {"$size": "$headcount"}, "avg_work_hours": {"$cond": [
            {"$gt": ["$work_hours_count", 0]}, {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None
        ]}}},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort": {"_id": 1}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": 1, "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1}}
    ]
    items = list(db.employees.aggregate(pipeline, allowDiskUse=True))
    for item in items:
        item["present_days"] = rounded(item["present_days"], 2)
        item["avg_work_hours"] = rounded(item["avg_work_hours"], 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    ensure_month(month)
    first_s, last_s = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first_s, "$lte": last_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
            "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}
    ]
    items = list(db.attendance_logs.aggregate(pipeline, allowDiskUse=True))
    return {"month": month, "items": items}




@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start_s = date_str_valid(from_date)
    end_s = date_str_valid(to_date)

    start = date.fromisoformat(start_s)
    end = date.fromisoformat(end_s)

    if end < start:
        raise HTTPException(422, "'to' must be on or after 'from'")

    if (end - start).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")

    # Fetch employees in the requested department.
    employees = list(
        db.employees.find(
            {"department": department},
            {"_id": 0, "emp_code": 1, "joined_on": 1},
        )
    )

    if not employees:
        raise HTTPException(404, "department not found")

    # Attendance records are fetched separately so employees without
    # attendance records are still included in the daily headcount.
    emp_codes = [emp["emp_code"] for emp in employees]

    attendance_records = list(
        db.attendance_logs.find(
            {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": start_s, "$lte": end_s},
            },
            {
                "_id": 0,
                "emp_code": 1,
                "date": 1,
                "status": 1,
                "late_minutes": 1,
                "half_day": 1,
            },
        )
    )

    # Index attendance records by date.
    records_by_date = {}
    for record in attendance_records:
        records_by_date.setdefault(record["date"], []).append(record)

    items = []
    current = start

    while current <= end:
        day_s = current.isoformat()

        # Count every employee who has joined by this date,
        # regardless of whether they have an attendance record.
        headcount = sum(
            1
            for emp in employees
            if emp["joined_on"] <= day_s
        )

        day_records = records_by_date.get(day_s, [])

        present_count = 0.0
        late_count = 0

        for record in day_records:
            if record.get("status") in PRESENCE:
                if record.get("half_day", False):
                    present_count += 0.5
                else:
                    present_count += 1.0

            if (record.get("late_minutes") or 0) > 0:
                late_count += 1

        is_working_day = current.weekday() < 5

        if is_working_day and headcount > 0:
            attendance_rate = present_count / headcount
        else:
            attendance_rate = None

        items.append(
            {
                "date": day_s,
                "is_working_day": is_working_day,
                "headcount": headcount,
                "present_count": rounded(present_count, 2),
                "late_count": late_count,
                "attendance_rate": rounded(attendance_rate, 4),
                "moving_avg_7d": None,
            }
        )

        current += timedelta(days=1)

    # Calculate the moving average over the current day and previous
    # six calendar days, ignoring None rates (weekends or zero headcount).
    for i, item in enumerate(items):
        window = items[max(0, i - 6):i + 1]
        rates = [
            row["attendance_rate"]
            for row in window
            if row["attendance_rate"] is not None
        ]

        if rates:
            item["moving_avg_7d"] = rounded(
                sum(rates) / len(rates), 4
            )

    return {"department": department, "items": items}


# ------------------------------- explain -------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, "unsupported endpoint")
    if endpoint in {"employee_monthly", "department_summary", "late_leaderboard"}:
        if not month:
            raise HTTPException(422, "month is required")
        ensure_month(month)
    if endpoint == "employee_monthly" and not emp_code:
        raise HTTPException(422, "emp_code is required")
    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")
        date_str_valid(from_date)
        date_str_valid(to_date)
    if endpoint == "attendance_list":
        q: dict[str, Any] = {}
        if emp_code: q["emp_code"] = emp_code
        if status: q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        exp = db.command("explain", {"find": "attendance_logs", "filter": q,
            "sort": {"date": -1, "emp_code": 1}, "skip": (page-1)*page_size, "limit": page_size},
            verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        first_s, last_s = month_bounds(month)
        pipeline = [{"$match": {"emp_code": emp_code, "date": {"$gte": first_s, "$lte": last_s}}},
                    {"$group": {"_id": "$emp_code", "count": {"$sum": 1}}}]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                         verbosity="executionStats")
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        first_s, last_s = month_bounds(month)
        match = {"joined_on": {"$lte": last_s}}
        if department: match["department"] = department
        pipeline = [{"$match": match}, {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "logs"}}]
        exp = db.command("explain", {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
                         verbosity="executionStats")
        collection = "employees"
    elif endpoint == "late_leaderboard":
        first_s, last_s = month_bounds(month)
        pipeline = [{"$match": {"date": {"$gte": first_s, "$lte": last_s}, "late_minutes": {"$gt": 0}}},
                    {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}}}]
        exp = db.command("explain", {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
                         verbosity="executionStats")
        collection = "attendance_logs"
    else:
        # Explain the indexed attendance lookup used by the trend pipeline.
        q = {"date": {"$gte": from_date, "$lte": to_date}}
        exp = db.command("explain", {"find": "attendance_logs", "filter": q, "sort": {"date": 1}},
                         verbosity="executionStats")
        collection = "attendance_logs"
    # Return raw explain in JSON-safe form; no MongoDB ObjectId/datetime leakage.
    return {"endpoint": endpoint, "collection": collection, "explain": __import__("json").loads(dumps(exp))}
