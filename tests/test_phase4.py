"""
Tests for Phase 4: Analytics Pipelines
- Employee monthly (R7 mid-month joiner, weekend exclusion for presence, R8 Decimal rounding)
- Department summary (R9 headcount with 0-log employee, joined_on cutoff, cents avg)
- Late leaderboard ($rank competition ranking, ties at cutoff, join dropping unknown employees)
- Department trend (gap filling with $densify, 7d moving average, unrounded daily rate, 92-day limit)
"""
from datetime import datetime, timezone
from fastapi.testclient import TestClient
import pytest

from app.main import app, db


TEST_EMPS = ["EMP8001", "EMP8002", "EMP8003", "EMP8010", "EMP8011", "EMP8012", "EMP8013"]


@pytest.fixture(autouse=True)
def clean_analytics_data():
    db.employees.delete_many({"emp_code": {"$in": TEST_EMPS + ["EMP_GHOST"]}})
    db.attendance_logs.delete_many({"emp_code": {"$in": TEST_EMPS + ["EMP_GHOST"]}})
    yield
    db.employees.delete_many({"emp_code": {"$in": TEST_EMPS + ["EMP_GHOST"]}})
    db.attendance_logs.delete_many({"emp_code": {"$in": TEST_EMPS + ["EMP_GHOST"]}})


def seed_analytics_data():
    # EMP8001: Mid-month joiner (2026-07-13, Monday) in AnalyticsQA
    db.employees.insert_one({
        "emp_code": "EMP8001", "name": "Mid Month", "email": "mid@example.com",
        "department": "AnalyticsQA", "shift_start": "09:30", "shift_end": "18:30",
        "joined_on": "2026-07-13", "created_at": datetime(2026, 7, 13, tzinfo=timezone.utc),
    })
    # EMP8002: Full-month employee in AnalyticsQA with ZERO logs
    db.employees.insert_one({
        "emp_code": "EMP8002", "name": "Zero Logs", "email": "zero@example.com",
        "department": "AnalyticsQA", "shift_start": "09:30", "shift_end": "18:30",
        "joined_on": "2026-07-01", "created_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
    })
    # EMP8003: Joined next month (August 2026) -> should be excluded from July summary
    db.employees.insert_one({
        "emp_code": "EMP8003", "name": "Next Month", "email": "next@example.com",
        "department": "AnalyticsQA", "shift_start": "09:30", "shift_end": "18:30",
        "joined_on": "2026-08-01", "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
    })

    # Attendance logs for EMP8001
    db.attendance_logs.insert_many([
        # 1. Mon 13 July: Weekday PRESENT full day (8.0 hours)
        {"emp_code": "EMP8001", "date": "2026-07-13", "status": "PRESENT", "work_hours": 8.0,
         "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
        # 2. Tue 14 July: Weekday WFH half day (3.5 hours, late 15)
        {"emp_code": "EMP8001", "date": "2026-07-14", "status": "WFH", "work_hours": 3.5,
         "late_minutes": 15, "overtime_minutes": 0, "half_day": True, "history": []},
        # 3. Wed 15 July: Weekday LEAVE
        {"emp_code": "EMP8001", "date": "2026-07-15", "status": "LEAVE", "work_hours": None,
         "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []},
        # 4. Sat 18 July: Weekend PRESENT (4.0 hours, late 20, overtime 45) -> presence +0, late/ot count!
        {"emp_code": "EMP8001", "date": "2026-07-18", "status": "PRESENT", "work_hours": 4.0,
         "late_minutes": 20, "overtime_minutes": 45, "half_day": True, "history": []},
    ])


def test_employee_monthly_analytics():
    seed_analytics_data()
    with TestClient(app) as client:
        # EMP8001 monthly report for 2026-07
        res = client.get("/analytics/employees/EMP8001/monthly?month=2026-07")
        assert res.status_code == 200
        d = res.json()
        assert d["emp_code"] == "EMP8001"
        assert d["month"] == "2026-07"
        # R7: working days from 2026-07-13 to 2026-07-31 = 15 Mon-Fri days
        assert d["working_days"] == 15
        # present_days: 1.0 (Mon) + 0.5 (Tue) + 0.0 (LEAVE) + 0.0 (Saturday) = 1.5
        assert d["present_days"] == 1.5
        assert d["leave_days"] == 1
        # late_count: Tue (15) + Sat (20) = 2
        assert d["late_count"] == 2
        assert d["total_late_minutes"] == 35
        # overtime_minutes: Sat (45) = 45
        assert d["total_overtime_minutes"] == 45
        # attendance_pct: 1.5 / 15 * 100 = 10.0%
        assert d["attendance_pct"] == 10.0

        # EMP8002 monthly report (zero logs)
        res_zero = client.get("/analytics/employees/EMP8002/monthly?month=2026-07")
        assert res_zero.status_code == 200
        z = res_zero.json()
        assert z["working_days"] == 23  # full month July 1-31
        assert z["present_days"] == 0.0
        assert z["leave_days"] == 0
        assert z["attendance_pct"] == 0.0

        # 404 for unknown employee
        assert client.get("/analytics/employees/UNKNOWN/monthly?month=2026-07").status_code == 404

        # 422 for invalid month
        assert client.get("/analytics/employees/EMP8001/monthly?month=2026-13").status_code == 422


def test_department_summary():
    seed_analytics_data()
    with TestClient(app) as client:
        res = client.get("/analytics/departments/summary?month=2026-07&department=AnalyticsQA")
        assert res.status_code == 200
        data = res.json()
        assert data["month"] == "2026-07"
        assert len(data["items"]) == 1
        dept = data["items"][0]
        assert dept["department"] == "AnalyticsQA"
        # R9 Headcount: EMP8001 and EMP8002 (EMP8003 joined in August, excluded) -> 2
        assert dept["headcount"] == 2
        # present_days: 1.5
        assert dept["present_days"] == 1.5
        # avg_work_hours: (8.0 + 3.5 + 4.0) / 3 records = 15.5 / 3 = 5.1666... -> 5.17
        assert dept["avg_work_hours"] == 5.17
        assert dept["late_count"] == 2
        assert dept["total_late_minutes"] == 35
        assert dept["leave_count"] == 1
        assert dept["on_duty_count"] == 0


def test_late_leaderboard_and_ranking():
    # Insert leaderboard test set
    db.employees.insert_many([
        {"emp_code": "EMP8010", "name": "Tied A", "email": "a@x.com", "department": "RankDept", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP8011", "name": "Tied B", "email": "b@x.com", "department": "RankDept", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP8012", "name": "Third", "email": "c@x.com", "department": "RankDept", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
        {"emp_code": "EMP8013", "name": "Fourth", "email": "d@x.com", "department": "RankDept", "shift_start": "09:30", "shift_end": "18:30", "joined_on": "2026-01-01"},
    ])
    db.attendance_logs.insert_many([
        {"emp_code": "EMP8010", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 50},
        {"emp_code": "EMP8011", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 50},
        {"emp_code": "EMP8012", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 30},
        {"emp_code": "EMP8013", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 20},
        # Ghost record without employee: join should drop it!
        {"emp_code": "EMP_GHOST", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 100},
    ])

    with TestClient(app) as client:
        # Full leaderboard for department
        res = client.get("/analytics/leaderboard/late?month=2026-07&department=RankDept&limit=10")
        assert res.status_code == 200
        items = res.json()["items"]
        # EMP_GHOST dropped -> exactly 4 items
        assert len(items) == 4
        # Competition ranks: 1, 1, 3, 4
        assert items[0]["rank"] == 1 and items[0]["emp_code"] == "EMP8010"
        assert items[1]["rank"] == 1 and items[1]["emp_code"] == "EMP8011"
        assert items[2]["rank"] == 3 and items[2]["emp_code"] == "EMP8012"
        assert items[3]["rank"] == 4 and items[3]["emp_code"] == "EMP8013"

        # Limit=1 test: both tied rank-1 employees must be returned (length = 2 > limit)
        res_lim1 = client.get("/analytics/leaderboard/late?month=2026-07&department=RankDept&limit=1")
        items_lim1 = res_lim1.json()["items"]
        assert len(items_lim1) == 2
        assert items_lim1[0]["rank"] == 1
        assert items_lim1[1]["rank"] == 1


def test_department_trend():
    seed_analytics_data()
    with TestClient(app) as client:
        # 1. Valid range 2026-07-13 to 2026-07-19 (7 days)
        res = client.get("/analytics/departments/AnalyticsQA/trend?from=2026-07-13&to=2026-07-19")
        assert res.status_code == 200
        data = res.json()
        assert data["department"] == "AnalyticsQA"
        items = data["items"]
        # Exactly 7 days generated
        assert len(items) == 7
        assert items[0]["date"] == "2026-07-13"
        assert items[-1]["date"] == "2026-07-19"

        # Check moving average is present
        assert "moving_avg_7d" in items[-1]

        # 2. Unknown department -> 404
        assert client.get("/analytics/departments/UnknownDept/trend?from=2026-07-01&to=2026-07-05").status_code == 404

        # 3. to < from -> 422
        assert client.get("/analytics/departments/AnalyticsQA/trend?from=2026-07-10&to=2026-07-05").status_code == 422

        # 4. Range > 92 days -> 422
        assert client.get("/analytics/departments/AnalyticsQA/trend?from=2026-01-01&to=2026-04-15").status_code == 422
