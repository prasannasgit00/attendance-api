# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (the uniqueness guarantee and the join key), `(department, emp_code)` for filtered lists and the trend, `(joined_on)` for the headcount scan when no department is given. `attendance_logs`: unique `(emp_code, date)` (one record per day, per-employee queries, lookups) and `(date desc, emp_code)` (the list sort and month scans). I rejected an index on `status`: it has five values, so it filters too little to pay for its write cost.
2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique index lets one succeed; the other gets `DuplicateKeyError`, which I turn into 409. There is no read before the write.
3. **Ties.** The pipeline ranks with `$rank`, which gives 1, 2, 2, 4, and only then keeps `rank <= limit`. Both tied people come back, so the list can be longer than `limit`.
4. **Headcount.** The summary starts from `employees` (joined by month end) and `$lookup`s their logs, so someone with no logs still produces a row that is counted.
5. **At 100x.** I would stop aggregating raw logs per request and keep a daily rollup per department, updated at punch time.
