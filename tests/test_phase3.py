"""
Tests for Phase 3: Punch-out, PATCH Regularize, concurrency and history audit trail.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fastapi.testclient import TestClient
import pytest

from app.main import app, db


@pytest.fixture(autouse=True)
def clean_test_data():
    db.employees.delete_many({"emp_code": {"$in": ["EMP9010", "EMP9011", "EMP9012", "EMP9013"]}})
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP9010", "EMP9011", "EMP9012", "EMP9013"]}})
    yield
    db.employees.delete_many({"emp_code": {"$in": ["EMP9010", "EMP9011", "EMP9012", "EMP9013"]}})
    db.attendance_logs.delete_many({"emp_code": {"$in": ["EMP9010", "EMP9011", "EMP9012", "EMP9013"]}})


def test_punch_out_flow_and_validation():
    with TestClient(app) as client:
        # Create employee
        client.post("/employees", json={
            "emp_code": "EMP9010",
            "name": "Out Tester",
            "email": "out@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })

        # 1. 404 unknown employee
        r_unk_emp = client.post("/attendance/punch-out", json={"emp_code": "EMP9999"})
        assert r_unk_emp.status_code == 404

        # 2. 404 no punch-in found
        r_no_pin = client.post("/attendance/punch-out", json={"emp_code": "EMP9010"})
        assert r_no_pin.status_code == 404

        # Punch in at 09:28 IST (2026-07-06 03:58:00 UTC) = 17833099800000 ms
        pin_ms = 17833099800000 // 10  # 1783309980000 ms
        # Wait, let's calculate exact ms:
        # 2026-07-06 03:58:00 UTC timestamp:
        pin_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
        pin_ms = int(pin_dt.timestamp() * 1000)
        client.post("/attendance/punch-in", json={"emp_code": "EMP9010", "punched_at": pin_ms})

        # 3. 422 bad time (<= punch_in)
        r_early_out = client.post("/attendance/punch-out", json={"emp_code": "EMP9010", "punched_at": pin_ms - 1000})
        # Note: punch_in is <= punched_at filter: if punched_at < pin, no punch_in <= punched_at is found -> 404
        assert r_early_out.status_code in (404, 422)

        # 4. 422 bad time (> 24 hours after punch_in)
        pout_25h = pin_ms + 25 * 3600 * 1000
        r_late_out = client.post("/attendance/punch-out", json={"emp_code": "EMP9010", "punched_at": pout_25h})
        assert r_late_out.status_code == 422

        # 5. Successful punch-out at 18:35 IST (13:05 UTC) = pin + 9h 7m = 32820s
        pout_dt = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
        pout_ms = int(pout_dt.timestamp() * 1000)
        r_ok = client.post("/attendance/punch-out", json={"emp_code": "EMP9010", "punched_at": pout_ms})
        assert r_ok.status_code == 200
        data = r_ok.json()
        assert data["work_hours"] == 9.12
        assert data["late_minutes"] == 0
        assert data["overtime_minutes"] == 0
        assert data["half_day"] is False
        assert data["punch_out"] == pout_ms

        # 6. 409 already punched out
        r_already = client.post("/attendance/punch-out", json={"emp_code": "EMP9010", "punched_at": pout_ms + 1000})
        assert r_already.status_code == 409


def test_concurrent_punch_out_race():
    with TestClient(app) as client:
        client.post("/employees", json={
            "emp_code": "EMP9011",
            "name": "Race Out",
            "email": "raceout@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })
        pin_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        pin_ms = int(pin_dt.timestamp() * 1000)
        client.post("/attendance/punch-in", json={"emp_code": "EMP9011", "punched_at": pin_ms})

        pout_ms = pin_ms + 9 * 3600 * 1000
        def post_out(_):
            return client.post("/attendance/punch-out", json={"emp_code": "EMP9011", "punched_at": pout_ms}).status_code

        with ThreadPoolExecutor(max_workers=12) as ex:
            statuses = list(ex.map(post_out, range(12)))

        statuses.sort()
        assert statuses == [200] + [409] * 11


def test_patch_regularize_and_history_appended():
    with TestClient(app) as client:
        # Create employee and punch in/out
        client.post("/employees", json={
            "emp_code": "EMP9012",
            "name": "Patch Tester",
            "email": "patch@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })
        pin_dt = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)  # 10:05 IST (late 35)
        pin_ms = int(pin_dt.timestamp() * 1000)
        client.post("/attendance/punch-in", json={"emp_code": "EMP9012", "punched_at": pin_ms})

        # PATCH 1: Regularize punch_in to 09:30 IST (04:00 UTC)
        new_in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        new_in_ms = int(new_in_dt.timestamp() * 1000)
        r_patch1 = client.patch(
            "/attendance/EMP9012/2026-07-06",
            json={
                "punch_in": new_in_ms,
                "reason": "Traffic jam caused biometric delay",
                "regularized_by": "Manager Alice",
            },
        )
        assert r_patch1.status_code == 200
        p1 = r_patch1.json()
        assert p1["late_minutes"] == 0  # was 35, now 0
        assert len(p1["history"]) == 1
        h1 = p1["history"][0]
        assert h1["by"] == "Manager Alice"
        assert h1["reason"] == "Traffic jam caused biometric delay"
        assert "late_minutes" in h1["changes"]
        assert h1["changes"]["late_minutes"] == {"from": 35, "to": 0}

        # PATCH 2: Add punch_out at 18:30 IST (13:00 UTC)
        new_out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
        new_out_ms = int(new_out_dt.timestamp() * 1000)
        r_patch2 = client.patch(
            "/attendance/EMP9012/2026-07-06",
            json={
                "punch_out": new_out_ms,
                "reason": "Forgot to punch out at end of shift",
                "regularized_by": "HR Bob",
            },
        )
        assert r_patch2.status_code == 200
        p2 = r_patch2.json()
        # History must be APPENDED, not overwritten
        assert len(p2["history"]) == 2
        assert p2["history"][0]["by"] == "Manager Alice"
        assert p2["history"][1]["by"] == "HR Bob"
        assert p2["work_hours"] == 9.0
        assert p2["half_day"] is False

        # PATCH No-Op -> 422
        r_noop = client.patch(
            "/attendance/EMP9012/2026-07-06",
            json={
                "status": "PRESENT",
                "reason": "Trying to update nothing here",
                "regularized_by": "Admin",
            },
        )
        assert r_noop.status_code == 422
        assert r_noop.json()["detail"] == "request changes nothing"


def test_patch_validation_errors():
    with TestClient(app) as client:
        client.post("/employees", json={
            "emp_code": "EMP9013",
            "name": "Val Emp",
            "email": "val@example.com",
            "department": "Engineering",
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })
        pin_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
        pin_ms = int(pin_dt.timestamp() * 1000)
        client.post("/attendance/punch-in", json={"emp_code": "EMP9013", "punched_at": pin_ms})

        # 1. 404 unknown employee
        assert client.patch("/attendance/EMP9999/2026-07-06", json={"reason": "test valid reason", "regularized_by": "me"}).status_code == 404

        # 2. 404 unknown record date
        assert client.patch("/attendance/EMP9013/2026-07-07", json={"reason": "test valid reason", "regularized_by": "me"}).status_code == 404

        # 3. ABSENT with punch times -> 422
        assert client.patch("/attendance/EMP9013/2026-07-06", json={
            "status": "ABSENT", "punch_in": pin_ms, "reason": "marking absent with punch", "regularized_by": "me"
        }).status_code == 422

        # 4. punch_in moved to a different attendance date -> 422
        next_day_in = pin_ms + 24 * 3600 * 1000
        assert client.patch("/attendance/EMP9013/2026-07-06", json={
            "punch_in": next_day_in, "reason": "moving date", "regularized_by": "me"
        }).status_code == 422

        # 5. punch_out <= punch_in -> 422
        assert client.patch("/attendance/EMP9013/2026-07-06", json={
            "punch_out": pin_ms - 1000, "reason": "bad time", "regularized_by": "me"
        }).status_code == 422
