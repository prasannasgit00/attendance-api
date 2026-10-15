# REVIEW.md

Defects found in the starter `app/main.py`, and how each was fixed in this submission.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Floors to whole minutes first, then tests `> 10`. A punch-in 10:01 to 10:59 minutes after start gives 10, which fails the test and returns 0. R2 says strictly more than 10:00 means late. | Shift 09:30, punch 09:40:01 returns 0, expected 10. | Test the seconds (`secs > 600`), then floor. |
| 2 | `compute_late_minutes` | `punch_in.replace(hour=..)` takes the shift start on the punch's own date and zone. Shift times are IST; punch is naive local or UTC. Overnight shifts start the previous day. | Server in UTC: a 09:28 IST punch looks like 03:58, so lateness is wrong or negative. A 03:00 punch on a 22:00 shift is not measured from the previous evening. | Shift start is built in IST on the attendance date from R1. |
| 3 | all helpers, `punch_in` | No truncation to whole seconds (R1). | 09:40:00.900 is treated as 09:40:00.9 and can tip a late check. | `instant()` floors epoch ms to whole seconds before anything else. |
| 4 | `compute_work_hours` | `round(x, 2)` on a float is half-to-even and float-fuzzy, not half-up (R4). No truncation. | 32850 s = 9.125 h returns 9.12, expected 9.13. | `Decimal(seconds)/3600` quantized with `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | No 30-minute minimum (R3). End is always on the punch date, so an overnight shift end is a day early. Mixes aware and naive datetimes (TypeError). | 18:40 punch-out returns 10 instead of 0; overnight 22:00-06:00 shift punched out at 06:40 next day returns about 1,480 minutes, expected 40. | Whole minutes, kept only if >= 30. End time is built in IST, plus one day for overnight shifts. All datetimes are aware. |
| 6 | whole file | `half_day` (R5) is never computed. There is no punch-out endpoint at all. | No way to close a record. | Added `POST /attendance/punch-out` with a conditional update and half-day logic. |
| 7 | `EmployeeIn` | No validation: `emp_code` format, lengths, email, date, `HH:MM`, `shift_start != shift_end`. | `joined_on="banana"` is accepted. | Patterns, lengths, a date validator and a model validator. Bad input returns 422. |
| 8 | `PunchInIn` | `punched_at` accepts seconds, negatives, numeric strings. `status` is a free string, so `ABSENT` or junk can be punched in. `if body.punched_at` treats 0 as missing. | `punched_at=1783312500` (seconds) lands in January 1970. | Strict int in the range 1e11 to 4.1e12 ms, status limited to PRESENT/WFH/ON_DUTY, `is None` checks. |
| 9 | `create_employee` | Check-then-insert race, no unique index. | Two concurrent creates both return 201. | Unique index on `emp_code`; `DuplicateKeyError` becomes 409. |
| 10 | `create_employee` | `created_at` is naive local `datetime.now()` and is returned as a datetime, not epoch ms. | Response has an ISO string, contract wants an integer. | Aware UTC time stored as BSON date, returned as epoch ms. |
| 11 | `list_employees` | `skip = page * page_size` skips the first page. | `?page=1` omits the first 20 employees. | `(page - 1) * page_size`. |
| 12 | `list_employees` | `total` counts the whole collection, ignoring `department`. No sort, so paging is unstable. No bounds on `page` or `page_size`. | `?department=Sales` reports the total of everyone. `page=0` gives a negative skip and a 500. | Count uses the filter, sort by `emp_code`, `page >= 1`, `1 <= page_size <= 100`. |
| 13 | `punch_in` | Unknown employee: `emp` is `None`, so `emp["shift_start"]` raises TypeError (500). | POST with an unknown `emp_code`. | Explicit 404. |
| 14 | `punch_in` | `datetime.fromtimestamp(ms / 1000)` uses server local time and gives a naive value. `ts.date()` is the server's date, not IST. No overnight rule. | A punch at 00:30 IST gets the previous date on a UTC server. | UTC-aware instant, IST date, overnight rule from R1. |
| 15 | `punch_in` | Check-then-insert race, so duplicates are possible. | Two simultaneous requests both return 201. | Unique `(emp_code, date)` index; `DuplicateKeyError` becomes 409. |
| 16 | `punch_in` | Response leaks `id` (`_id`) and returns datetimes. | Response has `id` and ISO times. | `ser_record` returns epoch ms and no `id`. |
| 17 | `list_attendance` | Loads the whole collection, sorts and slices in Python. | Slow and memory-heavy at 100,000 records. | `find` with sort, skip and limit in MongoDB, backed by an index. |
| 18 | `list_attendance` | Sort is by `date` only, so ties are unordered. No validation of dates, status, page, `date_from > date_to`. Leaks `id`. Legacy records missing `half_day` or `history` are returned as-is. | Unstable pages; `date_from=zzz` is accepted. | Sort `date desc, emp_code asc`, validation (422), missing fields defaulted in `ser_record`. |
| 19 | `/health` | Always returns ok. Never pings MongoDB. | Stop MongoDB and `/health` is still 200. | Pings the database and returns 503 on failure. |
| 20 | module setup | No indexes are created. `MongoClient` returns naive datetimes. No server selection timeout. | `explain` shows COLLSCAN. Time comparisons are off by the UTC offset. | `ensure_indexes()` at startup; `tz_aware=True`; 5 s timeout. |
| 21 | whole file | The TODO endpoints (punch-out, PATCH, analytics, explain) are missing. | 404 from the grader. | Implemented all of them. |

## Looked at and judged not a defect

- `load_dotenv()` does not override real environment variables, which is the behaviour the contract asks for.
- `doc.pop("_id")` after `insert_one`: the driver adds `_id` to the dict, so removing it before returning is correct.
- `status_code=201` on both creates, and `HTTPException(409, ...)` for duplicates, match the contract.
- Filtering `date` with string `$gte`/`$lte` works, because `YYYY-MM-DD` strings sort the same as dates.
