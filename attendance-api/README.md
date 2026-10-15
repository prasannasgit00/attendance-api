# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017"   # local: docker run -p 27017:27017 mongo:7
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```
Indexes are created at startup (idempotently). `/docs` shows the interactive API.

## What is implemented
- Employees: create (unique `emp_code` enforced by an index) and paginated list.
- Attendance: punch-in, punch-out, list, and PATCH regularization with an append-only `history`.
- Analytics: employee monthly summary, department summary, late leaderboard, department trend. Each is one aggregation pipeline.
- `/admin/explain/{endpoint}` runs `explain` with `executionStats` on the same query the endpoint builds.
- Business rules R1-R10 follow `openapi.yaml`. Rounding is half-up everywhere (Decimal in Python, exact decimal math in the trend pipeline).

## Design notes
- Instants are BSON dates in MongoDB (client uses `tz_aware=True`) and epoch milliseconds in the API.
- Concurrency relies on the database: unique indexes for punch-in and employee creation, conditional updates for punch-out and PATCH.
- See `DECISIONS.md` for index and design choices, and `REVIEW.md` for the starter's defects.

## Testing

Tested manually against a MongoDB Atlas free cluster (M0), using the nine sample
documents from the kit loaded with `sample_seed.py`. I called the endpoints
through the Swagger page at `/docs`.

Checked and working:
- `GET /health` returned `{"status":"ok"}`
- `GET /employees` and `GET /attendance` returned the sample data
- `GET /analytics/employees/{emp_code}/monthly` for EMP0001, 2026-07
- `GET /analytics/departments/summary` for 2026-07
- `GET /analytics/leaderboard/late` for 2026-07
- `GET /analytics/departments/{department}/trend` for Sales, 2026-07-01 to 2026-07-14
- `GET /admin/explain/department_trend`
- `POST /employees` with invalid input returns 422 (an impossible date was rejected)


## AI assistance

I used an AI assistant (Claude) to help write and review the code, and I tested
and checked the result myself.
