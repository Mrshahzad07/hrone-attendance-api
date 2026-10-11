"""
Performance and Explain plan verification on ~100k records.
Inserts 300 employees x 330 days (~99k attendance records) into attendance_perf database,
times all list and analytics endpoints, and asserts:
1. Every endpoint responds in under 1 second.
2. Every explain plan has IXSCAN and NO COLLSCAN.
"""
import json, os, sys, time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from fastapi.testclient import TestClient


sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Use attendance_perf database
os.environ["MONGO_DB"] = "attendance_perf"
from app.main import app, db, lifespan, IDX_EMP_DATE, IDX_DATE_EMP, IDX_JOINED


DEPARTMENTS = ["Engineering", "Sales", "Support", "Marketing", "HR"]
NUM_EMPLOYEES = 300
NUM_DAYS = 330  # 300 * 330 = 99,000 docs


def seed_perf_data():
    print(f"Checking existing data in {db.name}...")
    n_emp = db.employees.count_documents({})
    n_att = db.attendance_logs.count_documents({})
    if n_emp >= NUM_EMPLOYEES and n_att >= 90_000:
        print(f"Data already seeded ({n_emp} employees, {n_att} attendance logs). Skipping insertion.")
        return

    print("Dropping and seeding perf database...")
    db.employees.drop()
    db.attendance_logs.drop()

    # Re-create indexes
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", 1), ("emp_code", 1)])
    db.employees.create_index(list(IDX_JOINED.items()))
    db.attendance_logs.create_index(list(IDX_EMP_DATE.items()), unique=True)
    db.attendance_logs.create_index(list(IDX_DATE_EMP.items()))
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])

    # 1. Employees
    employees = []
    for i in range(1, NUM_EMPLOYEES + 1):
        emp_code = f"EMP{i:04d}"
        dept = DEPARTMENTS[i % len(DEPARTMENTS)]
        employees.append({
            "emp_code": emp_code,
            "name": f"Employee {i}",
            "email": f"emp{i}@example.com",
            "department": dept,
            "shift_start": "09:30",
            "shift_end": "18:30",
            "joined_on": "2025-01-01",
            "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        })
    db.employees.insert_many(employees)
    print(f"Inserted {len(employees)} employees.")

    # 2. Attendance records (~99k records)
    base_date = date(2025, 1, 1)
    batch = []
    total_inserted = 0

    for d_idx in range(NUM_DAYS):
        curr_date = base_date + timedelta(days=d_idx)
        d_str = curr_date.isoformat()
        is_weekday = curr_date.weekday() < 5

        for i in range(1, NUM_EMPLOYEES + 1):
            emp_code = f"EMP{i:04d}"
            # Mostly present, occasional late or leave
            if is_weekday:
                status = "PRESENT"
                wh = 9.0
                late = 15 if (i + d_idx) % 10 == 0 else 0
                ot = 30 if (i + d_idx) % 7 == 0 else 0
                half = False
            else:
                status = "PRESENT" if (i + d_idx) % 20 == 0 else "ABSENT"
                wh = 4.0 if status == "PRESENT" else None
                late = 0
                ot = 0
                half = True if status == "PRESENT" else False

            batch.append({
                "emp_code": emp_code,
                "date": d_str,
                "status": status,
                "punch_in": datetime.combine(curr_date, datetime.min.time(), tzinfo=timezone.utc),
                "punch_out": datetime.combine(curr_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=8) if wh else None,
                "work_hours": wh,
                "late_minutes": late,
                "overtime_minutes": ot,
                "half_day": half,
                "history": [],
            })

            if len(batch) >= 10_000:
                db.attendance_logs.insert_many(batch)
                total_inserted += len(batch)
                print(f"Inserted {total_inserted} logs...")
                batch = []

    if batch:
        db.attendance_logs.insert_many(batch)
        total_inserted += len(batch)
        print(f"Total inserted: {total_inserted} logs.")


def run_checks():
    seed_perf_data()

    endpoints_to_test = [
        ("GET /employees", "/employees?department=Engineering&page=1&page_size=20"),
        ("GET /attendance", "/attendance?date_from=2025-06-01&date_to=2025-06-30&page=1&page_size=20"),
        ("GET /analytics/employees/EMP0001/monthly", "/analytics/employees/EMP0001/monthly?month=2025-06"),
        ("GET /analytics/departments/summary", "/analytics/departments/summary?month=2025-06&department=Engineering"),
        ("GET /analytics/leaderboard/late", "/analytics/leaderboard/late?month=2025-06&limit=10&department=Engineering"),
        ("GET /analytics/departments/Engineering/trend", "/analytics/departments/Engineering/trend?from=2025-06-01&to=2025-06-30"),
    ]

    print("\n--- 1. Timing Endpoints on ~100k records ---")
    with TestClient(app) as client:
        for name, url in endpoints_to_test:
            t0 = time.perf_counter()
            resp = client.get(url)
            elapsed_ms = (time.perf_counter() - t0) * 1000
            assert resp.status_code == 200, f"{name} failed with {resp.status_code}: {resp.text}"
            print(f"{name:50} -> {resp.status_code} in {elapsed_ms:6.2f} ms")
            assert elapsed_ms < 1000, f"{name} took {elapsed_ms:.2f} ms (expected < 1000 ms)"

        print("\n--- 2. Verifying Explain Plans (IXSCAN present, COLLSCAN absent) ---")
        explain_calls = [
            ("attendance_list", "/admin/explain/attendance_list?emp_code=EMP0001&date_from=2025-06-01&date_to=2025-06-30"),
            ("employee_monthly", "/admin/explain/employee_monthly?emp_code=EMP0001&month=2025-06"),
            ("department_summary", "/admin/explain/department_summary?month=2025-06&department=Engineering"),
            ("late_leaderboard", "/admin/explain/late_leaderboard?month=2025-06&limit=10&department=Engineering"),
            ("department_trend", "/admin/explain/department_trend?department=Engineering&from=2025-06-01&to=2025-06-30"),
        ]

        for ep_name, url in explain_calls:
            resp = client.get(url)
            assert resp.status_code == 200, f"Explain for {ep_name} failed: {resp.text}"
            data = resp.json()
            raw_text = json.dumps(data["explain"])
            has_ixscan = "IXSCAN" in raw_text
            has_collscan = "COLLSCAN" in raw_text
            print(f"Explain {ep_name:22} | IXSCAN: {str(has_ixscan):5} | COLLSCAN: {str(has_collscan):5}")
            assert has_ixscan, f"{ep_name} missing IXSCAN in plan!"
            assert not has_collscan, f"{ep_name} contains COLLSCAN in plan!"

    print("\nAll performance and explain plan checks PASSED successfully!")


if __name__ == "__main__":
    run_checks()
