"""
Unit tests for Phase 1: Foundation and business rules R1-R5 vectors.
"""
from datetime import datetime, time, timedelta, timezone
from fastapi.testclient import TestClient
import pytest
from app.main import (
    IST,
    app,
    client,
    db,
    ms_to_dt,
    dt_to_ms,
    rhu,
    need_date,
    month_range,
    working_days_between,
    attendance_date,
    shift_start_dt,
    shift_end_dt,
    compute_late_minutes,
    compute_work_hours,
    compute_overtime,
    derive,
    EmployeeIn,
    PunchInIn,
    PunchOutIn,
    RegularizeIn,
)


def test_conversions_and_r1_truncation():
    # 09:40:00.900 ms -> stored whole seconds 09:40:00
    # Let's pick a known timestamp with 900 ms:
    # 2026-07-06 09:40:00.900 IST = 04:10:00.900 UTC
    dt_utc = datetime(2026, 7, 6, 4, 10, 0, 900000, tzinfo=timezone.utc)
    ms_with_900 = int(dt_utc.timestamp() * 1000)
    converted = ms_to_dt(ms_with_900)
    assert converted.microsecond == 0
    assert converted.second == 0
    assert converted.minute == 10
    assert converted.hour == 4

    # Back to ms
    assert dt_to_ms(converted) == int(datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)


def test_r1_shift_vectors():
    # Overnight Shift 22:00-06:00
    # in 21:55 on 6th -> date 6th
    in_2155 = datetime(2026, 7, 6, 21, 55, 0, tzinfo=IST)
    assert attendance_date(in_2155, "22:00", "06:00") == "2026-07-06"

    # in 00:30 on 7th -> date 6th
    in_0030 = datetime(2026, 7, 7, 0, 30, 0, tzinfo=IST)
    assert attendance_date(in_0030, "22:00", "06:00") == "2026-07-06"

    # in 06:00:00 on 7th -> date 7th (not earlier than end)
    in_0600 = datetime(2026, 7, 7, 6, 0, 0, tzinfo=IST)
    assert attendance_date(in_0600, "22:00", "06:00") == "2026-07-07"


def test_r2_late_minutes_vectors():
    # Shift 09:30
    date_str = "2026-07-06"
    # 09:40:00 -> 0 (exactly 10 min grace)
    t_94000 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    assert compute_late_minutes(t_94000, date_str, "09:30") == 0

    # 09:40:01 -> 10 (strictly more than 10 min -> floor minutes since shift_start = 10)
    t_94001 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(t_94001, date_str, "09:30") == 10

    # 10:15:59 -> 45
    t_101559 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(t_101559, date_str, "09:30") == 45

    # 09:00 -> 0 (early)
    t_090000 = datetime(2026, 7, 6, 9, 0, 0, tzinfo=IST)
    assert compute_late_minutes(t_090000, date_str, "09:30") == 0


def test_r3_overtime_vectors():
    date_str = "2026-07-06"
    # End 18:30:
    # out 18:59:59 -> 0
    out_185959 = datetime(2026, 7, 6, 18, 59, 59, tzinfo=IST)
    assert compute_overtime(out_185959, date_str, "09:30", "18:30") == 0

    # out 19:00:00 -> 30
    out_190000 = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
    assert compute_overtime(out_190000, date_str, "09:30", "18:30") == 30

    # out 19:10 -> 40
    out_191000 = datetime(2026, 7, 6, 19, 10, 0, tzinfo=IST)
    assert compute_overtime(out_191000, date_str, "09:30", "18:30") == 40

    # Overnight 22:00-06:00:
    # out 06:40 next day -> 40
    out_064000 = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
    assert compute_overtime(out_064000, date_str, "22:00", "06:00") == 40


def test_r4_work_hours_vectors():
    t_base = datetime(2026, 7, 6, 9, 0, 0, tzinfo=timezone.utc)
    # 28,818 s = 8.005 -> 8.01
    assert compute_work_hours(t_base, t_base + timedelta(seconds=28818)) == 8.01
    # 3,618 s = 1.005 -> 1.01
    assert compute_work_hours(t_base, t_base + timedelta(seconds=3618)) == 1.01
    # 12,600 s -> 3.5
    assert compute_work_hours(t_base, t_base + timedelta(seconds=12600)) == 3.50


def test_r5_half_day_vectors():
    emp = {"shift_start": "09:30", "shift_end": "18:30"}
    t_in = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)

    # 4.49 -> true
    t_out_449 = t_in + timedelta(seconds=int(4.49 * 3600))
    d1 = derive("PRESENT", t_in, t_out_449, "2026-07-06", emp)
    assert d1["work_hours"] == 4.49
    assert d1["half_day"] is True

    # 4.50 -> false
    t_out_450 = t_in + timedelta(seconds=int(4.50 * 3600))
    d2 = derive("PRESENT", t_in, t_out_450, "2026-07-06", emp)
    assert d2["work_hours"] == 4.50
    assert d2["half_day"] is False

    # 4.495 rounds to 4.50 -> false
    # 4.495 * 3600 = 16182 s
    t_out_4495 = t_in + timedelta(seconds=16182)
    d3 = derive("PRESENT", t_in, t_out_4495, "2026-07-06", emp)
    assert d3["work_hours"] == 4.50
    assert d3["half_day"] is False


def test_health_and_lifespan_indexes():
    with TestClient(app) as client_app:
        # GET /health returns 200 ok
        resp = client_app.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

        # Verify indexes created on database
        emp_indexes = db.employees.index_information()
        assert "emp_code_1" in emp_indexes
        assert emp_indexes["emp_code_1"]["unique"] is True

        att_indexes = db.attendance_logs.index_information()
        assert "emp_code_1_date_1" in att_indexes
        assert att_indexes["emp_code_1_date_1"]["unique"] is True
        assert "date_-1_emp_code_1" in att_indexes
        assert "emp_code_1_punch_in_-1" in att_indexes
        assert "status_1_date_-1_emp_code_1" in att_indexes
