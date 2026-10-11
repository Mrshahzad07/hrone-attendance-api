# DECISIONS.md

### 1. Indexes
- **`employees`**:
  - Unique `{emp_code: 1}`: Identity and race-safe creation.
  - `{department: 1, emp_code: 1}`: Serves department-filtered employee listings and trend lookups.
  - `{joined_on: 1, department: 1}`: Scans active employees for department headcount (`joined_on <= month_end`).
- **`attendance_logs`**:
  - Unique `{emp_code: 1, date: 1}`: One record per day guarantee; powers monthly queries, PATCH lookups, and `$lookup` joins.
  - `{date: -1, emp_code: 1}`: Global list ordering and date-range scans for summary and leaderboard.
  - `{emp_code: 1, punch_in: -1}`: Quickly locates the open record on punch-out.
  - `{status: 1, date: -1, emp_code: 1}`: Fast status-filtered queries.
- **Rejected Index**: A standalone single-field index on `status` was rejected because the 5 discrete status values lack selectivity; the compound index covers it cleanly.

### 2. Punch-in Race
Both requests compute the same attendance date and call `db.attendance_logs.insert_one()`. Because we avoid check-then-insert antipatterns, the unique `(emp_code, date)` compound index serializes the write directly in MongoDB. Exactly one insert succeeds with 201; the rival immediately triggers a `DuplicateKeyError`, caught and returned as HTTP 409.

### 3. Ties
We use standard competition ranking via `$rank` inside `$setWindowFields` (`1, 2, 2, 4`). The cutoff filter applies `rank <= limit`. If multiple employees tie at the cutoff rank, all are returned, meaning the result length can legitimately exceed `limit`. Rows are ordered by late minutes descending, with `emp_code` ascending breaking ties.

### 4. Headcount
The department summary aggregation pipeline begins at `employees` (filtering `joined_on <= month_end`), not `attendance_logs`. It joins logs using a `$lookup` sub-pipeline. Because each qualified employee generates an initial document regardless of log count, every active employee is counted exactly once (`$sum: 1`).

### 5. Change for 100x Data
At 100x scale, scanning millions of individual logs during analytics would degrade latency. I would introduce pre-aggregated monthly employee rollups updated transactionally during punch and regularization events, while sharding the `attendance_logs` collection by hashed `emp_code` or `date`.
