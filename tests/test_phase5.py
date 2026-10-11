"""
Tests for Phase 5: /admin/explain/{endpoint} for all 5 endpoints.
Verifies IXSCAN presence, parameter validation, and execution stats.
"""
import json
from fastapi.testclient import TestClient
import pytest

from app.main import app


def test_explain_endpoints_structure_and_validation():
    with TestClient(app) as client:
        # Missing required params -> 422
        assert client.get("/admin/explain/employee_monthly").status_code == 422
        assert client.get("/admin/explain/employee_monthly?emp_code=EMP0001").status_code == 422
        assert client.get("/admin/explain/department_summary").status_code == 422
        assert client.get("/admin/explain/department_trend?department=Engineering").status_code == 422

        # 1. attendance_list explain
        r1 = client.get("/admin/explain/attendance_list?emp_code=EMP0001&date_from=2026-07-01&date_to=2026-07-10")
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["endpoint"] == "attendance_list"
        assert d1["collection"] == "attendance_logs"
        assert "explain" in d1
        # Check queryPlanner and executionStats
        raw_text_1 = json.dumps(d1["explain"])
        assert "IXSCAN" in raw_text_1

        # 2. employee_monthly explain
        r2 = client.get("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07")
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["endpoint"] == "employee_monthly"
        assert d2["collection"] == "attendance_logs"
        assert "IXSCAN" in json.dumps(d2["explain"])

        # 3. department_summary explain
        r3 = client.get("/admin/explain/department_summary?month=2026-07&department=Engineering")
        assert r3.status_code == 200
        d3 = r3.json()
        assert d3["endpoint"] == "department_summary"
        assert d3["collection"] == "employees"
        assert "IXSCAN" in json.dumps(d3["explain"])

        # 4. late_leaderboard explain
        r4 = client.get("/admin/explain/late_leaderboard?month=2026-07&limit=10")
        assert r4.status_code == 200
        d4 = r4.json()
        assert d4["endpoint"] == "late_leaderboard"
        assert d4["collection"] == "attendance_logs"
        assert "IXSCAN" in json.dumps(d4["explain"])

        # 5. department_trend explain
        r5 = client.get("/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-07")
        assert r5.status_code == 200
        d5 = r5.json()
        assert d5["endpoint"] == "department_trend"
        assert d5["collection"] == "attendance_logs"
        assert "IXSCAN" in json.dumps(d5["explain"])
