import json, os, re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional
from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))  # R1
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
GRACE_SECONDS = 10 * 60  # R2
OVERTIME_MIN_MINUTES = 30  # R3
HALF_DAY_HOURS = 4.50  # R5
MAX_SHIFT_HOURS = 24
PRESENT = ["PRESENT", "WFH", "ON_DUTY"]  # R6
WEEKDAYS = [2, 3, 4, 5, 6]  # R7: Mongo $dayOfWeek


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)  # R1


def dt_to_ms(dt):
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def rhu(x, places=2) -> float:
    q = Decimal(1).scaleb(-places)
    return float(Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP))  # R8


def need_date(s: str, name="date") -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s or ""):
        raise HTTPException(422, f"invalid {name}, expected YYYY-MM-DD")
    try:
        return date.fromisoformat(s)
    except ValueError:
        raise HTTPException(422, f"invalid {name}")


def month_range(month: str):
    y, m = int(month[:4]), int(month[5:7])
    try:
        first = date(y, m, 1)
        last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    except (ValueError, OverflowError):
        raise HTTPException(422, "invalid month")
    return first, last


def working_days_between(a: date, b: date) -> int:
    n, d = 0, a
    while d <= b:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n  # R7


def _hm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < _hm(shift_end):
        d -= timedelta(days=1)
    return d.isoformat()  # R1


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    return datetime.combine(date.fromisoformat(date_str), _hm(shift_start), tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    end = datetime.combine(date.fromisoformat(date_str), _hm(shift_end), tzinfo=IST)
    return end + timedelta(days=1) if is_overnight(shift_start, shift_end) else end


def compute_late_minutes(punch_in, date_str, shift_start) -> int:
    delta = int((punch_in - shift_start_dt(date_str, shift_start)).total_seconds())
    return delta // 60 if delta > GRACE_SECONDS else 0  # R2


def compute_work_hours(punch_in, punch_out) -> float:
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))  # R4


def compute_overtime(punch_out, date_str, shift_start, shift_end) -> int:
    delta = int((punch_out - shift_end_dt(date_str, shift_start, shift_end)).total_seconds())
    mins = delta // 60
    return mins if mins >= OVERTIME_MIN_MINUTES else 0  # R3


def derive(status, punch_in, punch_out, date_str, emp) -> dict:
    if status not in PRESENT or punch_in is None:
        return dict(work_hours=None, late_minutes=0, overtime_minutes=0, half_day=False)
    late = compute_late_minutes(punch_in, date_str, emp["shift_start"])
    if punch_out is None:
        return dict(work_hours=None, late_minutes=late, overtime_minutes=0, half_day=False)
    wh = compute_work_hours(punch_in, punch_out)
    ot = compute_overtime(punch_out, date_str, emp["shift_start"], emp["shift_end"])
    return dict(work_hours=wh, late_minutes=late, overtime_minutes=ot, half_day=wh < HALF_DAY_HOURS)  # R5


EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
HHMM = r"^([01]\d|2[0-3]):[0-5]\d$"
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _date_ok(cls, v):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def emp_out(e) -> dict:
    return {
        "emp_code": e["emp_code"],
        "name": e["name"],
        "email": e["email"],
        "department": e["department"],
        "shift_start": e["shift_start"],
        "shift_end": e["shift_end"],
        "joined_on": e["joined_on"],
        "created_at": dt_to_ms(e.get("created_at")),
    }


def hist_out(h) -> dict:
    ch = {}
    for k, v in h.get("changes", {}).items():
        if k in ("punch_in", "punch_out"):
            ch[k] = {"from": dt_to_ms(v.get("from")), "to": dt_to_ms(v.get("to"))}
        else:
            ch[k] = v
    return {"at": dt_to_ms(h["at"]), "by": h["by"], "reason": h["reason"], "changes": ch}


def att_out(d) -> dict:
    return {
        "emp_code": d["emp_code"],
        "date": d["date"],
        "status": d["status"],
        "punch_in": dt_to_ms(d.get("punch_in")),
        "punch_out": dt_to_ms(d.get("punch_out")),
        "work_hours": d.get("work_hours"),
        "late_minutes": d.get("late_minutes", 0),
        "overtime_minutes": d.get("overtime_minutes", 0),
        "half_day": d.get("half_day", False),
        "history": [hist_out(h) for h in d.get("history", [])],
    }


IDX_EMP_DATE = {"emp_code": 1, "date": 1}
IDX_DATE_EMP = {"date": -1, "emp_code": 1}
IDX_JOINED = {"joined_on": 1, "department": 1}


@asynccontextmanager
async def lifespan(app):
    E, A = db.employees, db.attendance_logs
    E.create_index("emp_code", unique=True)
    E.create_index([("department", 1), ("emp_code", 1)])
    E.create_index(list(IDX_JOINED.items()))
    A.create_index(list(IDX_EMP_DATE.items()), unique=True)
    A.create_index(list(IDX_DATE_EMP.items()))
    A.create_index([("emp_code", 1), ("punch_in", -1)])
    A.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "mongodb unavailable")
    return {"status": "ok"}


Page = Query(1, ge=1)  # R10
PageSize = Query(20, ge=1, le=100)  # R10


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return emp_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Page, page_size: int = PageSize):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)  # R10
    cur = db.employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [emp_out(e) for e in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])  # R1
    doc = {"emp_code": body.emp_code, "date": d, "status": body.status, "punch_in": ts, "punch_out": None, "history": []}
    doc.update(derive(body.status, ts, None, d, emp))  # R2
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return att_out(doc)


def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    lo = need_date(date_from, "date_from") if date_from else None
    hi = need_date(date_to, "date_to") if date_to else None
    if lo and hi and lo > hi:
        raise HTTPException(422, "date_from must not be after date_to")
    if lo or hi:
        q["date"] = {}
        if lo:
            q["date"]["$gte"] = date_from
        if hi:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


ATT_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: int = Page,
    page_size: int = PageSize,
):
    q = attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    cur = db.attendance_logs.find(q).sort(ATT_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [att_out(d) for d in cur], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = rec["punch_in"]
    if ts <= pin or ts - pin > timedelta(hours=MAX_SHIFT_HOURS):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")
    derived = derive(rec["status"], pin, ts, rec["date"], emp)  # R3, R4, R5
    res = db.attendance_logs.update_one(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
    )
    if res.matched_count == 0:
        raise HTTPException(409, "already punched out")
    rec.update(punch_out=ts, **derived)
    return att_out(rec)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    need_date(date, "date")
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no record for that date")

    fields = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
    old = {k: rec.get(k, 0 if k.endswith("minutes") else False if k == "half_day" else None) for k in fields}
    status = body.status or rec["status"]

    if status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if new_in is None:
            raise HTTPException(422, "a presence status requires punch_in")
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != rec["date"]:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=MAX_SHIFT_HOURS)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    new = {
        "status": status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derive(status, new_in, new_out, rec["date"], emp),
    }
    changes = {k: {"from": old[k], "to": new[k]} for k in fields if new[k] != old[k]}
    if not changes:
        raise HTTPException(422, "request changes nothing")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    cas = {
        "_id": rec["_id"],
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
    }  # compare-and-set
    res = db.attendance_logs.update_one(cas, {"$set": new, "$push": {"history": entry}})
    if res.matched_count == 0:
        raise HTTPException(409, "record was modified concurrently, retry")

    rec.update(new)
    rec["history"] = rec.get("history", []) + [entry]
    return att_out(rec)


Month = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
IS_PRESENT = {"$in": ["$status", PRESENT]}  # R6
DOW = {"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}
LATE = {"$ifNull": ["$late_minutes", 0]}
PRESENT_VALUE = {
    "$cond": [
        {"$and": [IS_PRESENT, {"$in": [DOW, WEEKDAYS]}]},
        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
        0,
    ]
}


def monthly_pipeline(emp_code: str, first: str, last: str):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": PRESENT_VALUE},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": LATE},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Month):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    first, last = month_range(month)
    start = max(first, date.fromisoformat(emp["joined_on"]))  # R7
    working = working_days_between(start, last) if start <= last else 0
    rows = list(
        db.attendance_logs.aggregate(
            monthly_pipeline(emp_code, first.isoformat(), last.isoformat()),
            hint=IDX_EMP_DATE,
        )
    )
    s = rows[0] if rows else {}
    present = s.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working,
        "present_days": rhu(present),
        "leave_days": s.get("leave_days", 0),
        "late_count": s.get("late_count", 0),
        "total_late_minutes": s.get("total_late_minutes", 0),
        "total_overtime_minutes": s.get("total_overtime_minutes", 0),
        "attendance_pct": rhu(Decimal(str(present)) / working * 100) if working else None,
    }


def dept_summary_pipeline(first: str, last: str, department: Optional[str] = None):
    match = {"joined_on": {"$lte": last}}  # R9
    if department is not None:
        match["department"] = department
    has_hours = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": first, "$lte": last}}},
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {"$sum": PRESENT_VALUE},
                            "cents": {
                                "$sum": {
                                    "$cond": [
                                        has_hours,
                                        {"$round": [{"$multiply": ["$work_hours", 100]}, 0]},
                                        0,
                                    ]
                                }
                            },
                            "n_hours": {"$sum": {"$cond": [has_hours, 1, 0]}},
                            "late_count": {"$sum": {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}},
                            "total_late": {"$sum": LATE},
                            "leave": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                            "on_duty": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                        }
                    },
                ],
                "as": "s",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$sum": "$s.present_days"}},
                "cents": {"$sum": {"$sum": "$s.cents"}},
                "n_hours": {"$sum": {"$sum": "$s.n_hours"}},
                "late_count": {"$sum": {"$sum": "$s.late_count"}},
                "total_late": {"$sum": {"$sum": "$s.total_late"}},
                "leave": {"$sum": {"$sum": "$s.leave"}},
                "on_duty": {"$sum": {"$sum": "$s.on_duty"}},
            }
        },
        {"$sort": {"_id": 1}},
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str = Month, department: Optional[str] = None):
    first, last = month_range(month)
    rows = db.employees.aggregate(
        dept_summary_pipeline(first.isoformat(), last.isoformat(), department),
        hint=IDX_JOINED,
    )
    items = []
    for r in rows:
        avg = None
        if r["n_hours"]:
            avg = float((Decimal(r["cents"]) / r["n_hours"] / 100).quantize(Decimal("0.01"), ROUND_HALF_UP))
        items.append({
            "department": r["_id"],
            "headcount": r["headcount"],
            "present_days": rhu(r["present_days"]),
            "avg_work_hours": avg,
            "late_count": r["late_count"],
            "total_late_minutes": r["total_late"],
            "leave_count": r["leave"],
            "on_duty_count": r["on_duty"],
        })
    return {"month": month, "items": items}


def leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str] = None):
    pl = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": LATE},
                "late_count": {"$sum": {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},
    ]
    if department is not None:
        pl.append({"$match": {"e.department": department}})
    pl += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$e.name",
                "department": "$e.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pl


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str = Month, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    first, last = month_range(month)
    items = list(
        db.attendance_logs.aggregate(
            leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department),
            hint=IDX_DATE_EMP,
        )
    )
    return {"month": month, "items": items}


def trend_inputs(department: str, frm: str, to: str):
    a, b = need_date(frm, "from"), need_date(to, "to")
    if b < a:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (b - a).days + 1 > 92:
        raise HTTPException(422, "range exceeds 92 days")
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "unknown department")
    return a, b, [e["emp_code"] for e in emps], [e["joined_on"] for e in emps]


def trend_pipeline(codes: list, joined: list, a: date, b: date):
    lo = datetime(a.year, a.month, a.day, tzinfo=timezone.utc)
    hi = datetime(b.year, b.month, b.day, tzinfo=timezone.utc) + timedelta(days=1)
    half = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": a.isoformat(), "$lte": b.isoformat()}}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": {"$cond": [IS_PRESENT, half, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [LATE, 0]}, 1, 0]}},
            }
        },
        {"$unionWith": {"pipeline": [{"$documents": [{"_id": a.isoformat(), "present_count": 0, "late_count": 0}]}]}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$addFields": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {
            "$addFields": {
                "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, WEEKDAYS]},
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": {"$literal": joined},
                            "as": "j",
                            "cond": {"$lte": ["$$j", "$date"]},
                        }
                    }
                }
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {"$sort": {"day": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    a, b, codes, joined = trend_inputs(department, from_, to)
    rows = db.attendance_logs.aggregate(trend_pipeline(codes, joined, a, b), hint=IDX_EMP_DATE)
    items = []
    for r in rows:
        r["present_count"] = rhu(r["present_count"])
        r["attendance_rate"] = rhu(r["attendance_rate"], 4) if r["attendance_rate"] is not None else None
        r["moving_avg_7d"] = rhu(r["moving_avg_7d"], 4) if r["moving_avg_7d"] is not None else None
        items.append(r)
    return {"department": department, "items": items}


def run_explain(coll, find=None, pipeline=None, hint=None):
    inner = {"find": coll, **find} if find is not None else {"aggregate": coll, "pipeline": pipeline, "cursor": {}}
    if hint:
        inner["hint"] = hint
    out = db.command({"explain": inner, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(out))


def _req(v, name):
    if v is None:
        raise HTTPException(422, f"{name} is required")
    return v


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Page,
    page_size: int = PageSize,
):
    if endpoint == "attendance_list":
        q = attendance_filter(emp_code, date_from, date_to, status)
        sort = {"date": -1, "emp_code": 1}
        ex = run_explain(
            "attendance_logs",
            find={"filter": q, "sort": sort, "skip": (page - 1) * page_size, "limit": page_size},
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": ex}

    if endpoint == "employee_monthly":
        first, last = month_range(_req(month, "month"))
        pl = monthly_pipeline(_req(emp_code, "emp_code"), first.isoformat(), last.isoformat())
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": run_explain("attendance_logs", pipeline=pl, hint=IDX_EMP_DATE),
        }

    if endpoint == "department_summary":
        first, last = month_range(_req(month, "month"))
        pl = dept_summary_pipeline(first.isoformat(), last.isoformat(), department)
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": run_explain("employees", pipeline=pl, hint=IDX_JOINED),
        }

    if endpoint == "late_leaderboard":
        first, last = month_range(_req(month, "month"))
        pl = leaderboard_pipeline(first.isoformat(), last.isoformat(), limit, department)
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": run_explain("attendance_logs", pipeline=pl, hint=IDX_DATE_EMP),
        }

    a, b, codes, joined = trend_inputs(_req(department, "department"), _req(from_, "from"), _req(to, "to"))
    return {
        "endpoint": endpoint,
        "collection": "attendance_logs",
        "explain": run_explain("attendance_logs", pipeline=trend_pipeline(codes, joined, a, b), hint=IDX_EMP_DATE),
    }
