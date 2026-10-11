"""
Tests for Phase 2: Employees (POST/GET), punch-in, list attendance, pagination, and concurrency races.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fastapi.testclient import TestClient
import httpx
import pytest

from app.main import app, db


@pytest.fixture(autouse=True)
def clean_test_data():
    """Ensure test employees and logs are cleaned up before/after each test."""
    db.employees.delete_many({"emp_code": {"$in": ["EMP9001", "EMP9002", "EMP9003", "EMP9004", "EMP9005"]}})
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP9001", "EMP9002", "EMP9003", "EMP9004", "EMP9005"]}})
    yield
    db.employees.delete_many({"emp_code": {"$in": ["EMP9001", "EMP9002", "EMP9003", "EMP9004", "EMP9005"]}})
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP9001", "EMP9002", "EMP9003", "EMP9004", "EMP9005"]}})


def test_create_and_list_employees():
    with TestClient(app) as client:
        # Create valid employee
        payload = {
            "emp_code": "EMP9001",
            "name": "Jane Tester",
            "email": "jane@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        }
        resp = client.post("/employees", json=payload)
        assert resp.status_code == 201
        data = resp.json()
        assert data["emp_code"] == "EMP9001"
        assert "_id" not in data
        assert isinstance(data["created_at"], int)

        # Duplicate create returns 409
        dup_resp = client.post("/employees", json=payload)
        assert dup_resp.status_code == 409
        assert dup_resp.json()["detail"] == "emp_code already exists"

        # List employees
        list_resp = client.get("/employees?department=Engineering")
        assert list_resp.status_code == 200
        ldata = list_resp.json()
        assert ldata["page"] == 1
        assert ldata["page_size"] == 20
        assert ldata["total"] >= 1
        assert any(e["emp_code"] == "EMP9001" for e in ldata["items"])


def test_employee_validation_errors():
    with TestClient(app) as client:
        # Invalid emp_code
        r1 = client.post("/employees", json={
            "emp_code": "BADCODE", "name": "A", "email": "a@b.com", "department": "D", "joined_on": "2026-01-01"
        })
        assert r1.status_code == 422

        # Invalid email
        r2 = client.post("/employees", json={
            "emp_code": "EMP9001", "name": "A", "email": "invalid-email", "department": "D", "joined_on": "2026-01-01"
        })
        assert r2.status_code == 422

        # shift_start == shift_end
        r3 = client.post("/employees", json={
            "emp_code": "EMP9001", "name": "A", "email": "a@b.com", "department": "D",
            "shift_start": "09:00", "shift_end": "09:00", "joined_on": "2026-01-01"
        })
        assert r3.status_code == 422

        # Invalid joined_on date (e.g. Feb 30)
        r4 = client.post("/employees", json={
            "emp_code": "EMP9001", "name": "A", "email": "a@b.com", "department": "D",
            "joined_on": "2026-02-30"
        })
        assert r4.status_code == 422

        # page = 0
        r5 = client.get("/employees?page=0")
        assert r5.status_code == 422

        # page_size = 101
        r6 = client.get("/employees?page_size=101")
        assert r6.status_code == 422


def test_concurrent_create_employee_race():
    payload = {
        "emp_code": "EMP9002",
        "name": "Race Employee",
        "email": "race@example.com",
        "department": "QA",
        "joined_on": "2026-01-01",
    }
    with TestClient(app) as client:
        def post_one(_):
            return client.post("/employees", json=payload).status_code

        with ThreadPoolExecutor(max_workers=12) as ex:
            statuses = list(ex.map(post_one, range(12)))

        statuses.sort()
        assert statuses == [201] + [409] * 11


def test_punch_in_and_validation():
    with TestClient(app) as client:
        # Create employee first
        client.post("/employees", json={
            "emp_code": "EMP9003",
            "name": "Worker",
            "email": "w@example.com",
            "department": "Ops",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })

        # Punch in for unknown employee -> 404
        r_404 = client.post("/attendance/punch-in", json={"emp_code": "EMP9999"})
        assert r_404.status_code == 404

        # Invalid status for punch in (e.g. ABSENT or LEAVE) -> 422
        r_bad_status = client.post("/attendance/punch-in", json={"emp_code": "EMP9003", "status": "ABSENT"})
        assert r_bad_status.status_code == 422

        # Invalid punched_at (seconds instead of ms, e.g. 1783312500 < 100_000_000_000) -> 422
        r_sec = client.post("/attendance/punch-in", json={"emp_code": "EMP9003", "punched_at": 1783312500})
        assert r_sec.status_code == 422

        # Valid punch in
        # 1783312500000 ms = 2026-07-06 04:35:00 UTC = 10:05:00 IST (late 35 min)
        ts_ms = 1783312500000
        r_ok = client.post("/attendance/punch-in", json={"emp_code": "EMP9003", "punched_at": ts_ms})
        assert r_ok.status_code == 201
        att = r_ok.json()
        assert att["emp_code"] == "EMP9003"
        assert att["date"] == "2026-07-06"
        assert att["late_minutes"] == 35
        assert att["work_hours"] is None
        assert att["punch_out"] is None
        assert "_id" not in att
        assert "id" not in att

        # Duplicate punch in for same day -> 409
        r_dup = client.post("/attendance/punch-in", json={"emp_code": "EMP9003", "punched_at": ts_ms + 1000})
        assert r_dup.status_code == 409


def test_concurrent_punch_in_race():
    with TestClient(app) as client:
        # Create employee
        client.post("/employees", json={
            "emp_code": "EMP9004",
            "name": "Racer In",
            "email": "racer@example.com",
            "department": "Ops",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })

        ts_ms = 1783312500000
        def punch_one(_):
            return client.post("/attendance/punch-in", json={"emp_code": "EMP9004", "punched_at": ts_ms}).status_code

        with ThreadPoolExecutor(max_workers=12) as ex:
            statuses = list(ex.map(punch_one, range(12)))

        statuses.sort()
        assert statuses == [201] + [409] * 11


def test_list_attendance_and_filters():
    with TestClient(app) as client:
        # List attendance
        r = client.get("/attendance")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        assert "total" in data

        # Invalid date_from > date_to -> 422
        r_bad = client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
        assert r_bad.status_code == 422

        # Invalid date format -> 422
        r_bad2 = client.get("/attendance?date_from=invalid-date")
        assert r_bad2.status_code == 422

        # page = 0 -> 422
        r_page0 = client.get("/attendance?page=0")
        assert r_page0.status_code == 422

        # page_size = 101 -> 422
        r_size101 = client.get("/attendance?page_size=101")
        assert r_size101.status_code == 422
