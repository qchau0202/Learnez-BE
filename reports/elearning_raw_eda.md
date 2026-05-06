# EDA report: MongoDB raw simulation layer

- **Database:** `elearning_raw`
- **Generated (UTC):** 2026-05-03T11:04:37+00:00
- **Collections:** `activity_events`, `assessment_events`, `attendance_events`, `simulation_users`

---

## Cross-collection summary

- **Simulation students (role_id=3):** 1,037
- **Simulation lecturers (role_id=2):** 43

- Join raw events to labels: `simulation_users.user_id` ↔ `*.user_id`; use `persona` as a **simulation ground-truth behavior segment** (not a production label).

---

## Data quality checks

- **activity_events** — documents with missing `user_id`: 0
- **assessment_events** — documents with missing `user_id`: 0
- **attendance_events** — documents with missing `user_id`: 0
- **simulation_users** — documents with missing `user_id`: 0

### idempotency_key cardinality (events)

- **activity_events:** documents ≈ 67,982, distinct `idempotency_key` ≈ 67,982
- **assessment_events:** documents ≈ 16,390, distinct `idempotency_key` ≈ 16,390
- **attendance_events:** documents ≈ 28,523, distinct `idempotency_key` ≈ 28,523

---

## activity_events

- **Estimated documents:** 67,982
- **Storage (collStats size):** 27.92 MiB

- **event_time range:** 2026-01-11T08:44:28.919000+00:00 → 2026-06-11T01:46:21.918000+00:00

### event_type distribution

| event_type | count |
|---|---:|
| `login` | 42,934 |
| `page_view` | 24,008 |
| `session_heartbeat` | 1,040 |

### Top course_id (activity)

| course_id | events |
|---|---:|
| 1 | 6,647 |
| 3 | 6,449 |
| 5 | 6,064 |
| 15 | 5,513 |
| 11 | 5,217 |
| 13 | 5,034 |
| 9 | 4,803 |
| 6 | 4,551 |
| 7 | 4,432 |
| 14 | 4,249 |
| 10 | 4,121 |
| 12 | 4,082 |
| 8 | 3,736 |
| 2 | 1,736 |
| 4 | 1,348 |

- **Distinct user_id:** 1,072

### Events per user (activity only) — deciles

*One pass per user (may take a minute on large collections).*

| percentile | events / user (activity) |
|---|---:|
| min | 1.0 |
| p10 | 20.0 |
| p25 | 31.0 |
| p50 | 58.0 |
| p75 | 85.0 |
| p90 | 121.0 |
| max | 198.0 |

---

## assessment_events

- **Estimated documents:** 16,390
- **Storage (collStats size):** 7.73 MiB

- **event_time range:** 2026-01-06T23:45:41.494000+00:00 → 2026-05-09T01:10:55.489000+00:00

### event_type distribution

| event_type | count |
|---|---:|
| `submission_created` | 8,351 |
| `graded` | 8,039 |

### timing_label (submission-related rows)

| timing_label | count |
|---|---:|
| `on_time` | 9,810 |
| `late` | 3,746 |
| `early` | 2,834 |

### final_score (non-null)

- **count:** 15,120
- **min / max / mean:** 3 / 10 / 6.9836

- **Distinct user_id:** 1,046

---

## attendance_events

- **Estimated documents:** 28,523
- **Storage (collStats size):** 12.55 MiB

- **event_time range:** 2026-05-01T00:00:05.455000+00:00 → 2026-07-22T01:29:29.255000+00:00

### status distribution

| status | count |
|---|---:|
| `Present` | 22,885 |
| `Late` | 2,872 |
| `Absent` | 2,766 |

### event_type distribution

| event_type | count |
|---|---:|
| `session_attended` | 25,757 |
| `session_absent` | 2,766 |

---

## simulation_users

- **Estimated documents:** 1,080
- **Storage (collStats size):** 0.51 MiB

### role_id

| role_id | count |
|---|---:|
| 2 | 43 |
| 3 | 1,037 |

### persona

| persona | count |
|---|---:|
| `steady` | 352 |
| `uneven` | 210 |
| `struggling` | 160 |
| `star` | 151 |
| `sparse` | 124 |
| `lecturer` | 43 |
| `dormant` | 40 |

### department_id (top 20)

| department_id | count |
|---|---:|
| 4 | 288 |
| 1 | 286 |
| 2 | 256 |
| 3 | 250 |

### current_gpa (students only, if present)

- **count:** 1,037
- **min / max / mean:** 5.039347138034169 / 9.497932445798499 / 7.3286

---

## Interpretation notes

- **Time axis:** Use `event_time` for behavioral sequencing; `created_at` may mirror it when data was clock-warped at ingest.
- **Personas** (`star`, `steady`, …) are useful for **sanity checks** and stratified evaluation; replace with real outcomes before production decisions.
- **assessment_events** mixes `submission_created` and `graded`; aggregate features should count submissions once (see weekly feature job).
