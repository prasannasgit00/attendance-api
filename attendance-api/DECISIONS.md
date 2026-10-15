1. **Indexes.** For employees I made a unique index on emp_code, one on
   (department, emp_code) and one on joined_on. For attendance_logs I made a
   unique index on (emp_code, date) and one on (date descending, emp_code). The
   unique ones stop duplicates. The others keep lists and monthly reports fast.
   I did not index status. It has only five values, so it would not narrow the
   search much and would slow down writes.

2. **Punch-in race.** Both requests work out the same date and try to insert the
   same record. The unique index lets only one insert succeed. The other one
   gets a duplicate-key error and I return 409. I never check first and then
   insert, because two requests could both pass the check.

3. **Ties.** I rank everyone first (1, 2, 2, 4) and only then keep rank <= limit.
   So if two people tie at the cutoff, both are returned, and the list can be
   longer than the limit.

4. **Headcount.** The department summary starts from the employees collection
   and then looks up their logs. So an employee with no logs still shows up and
   is counted.

5. **At 100x data.** I would stop calculating from the raw logs on every request.
   I would keep a daily summary per department, updated when someone punches in
   or out, and read the reports from that.
