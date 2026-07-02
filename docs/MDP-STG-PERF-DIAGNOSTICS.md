# MDP STG Performance Diagnostics — Quick Reference

**Log path (Azure STG):** `/home/LogFiles/mdp/server-log.log`  
**Analytics:** `/home/LogFiles/mdp/user_analytics.log`  
**Restarts ledger:** `/home/LogFiles/mdp/restarts.log`  
**Local dev:** `server-logs/server-log.log`

After deploy, SSH or Kudu → run these greps. Use `-a` for binary-safe reads on rotated logs.

---

## 1. Boot & liveness (is the app healthy?)

```bash
grep -a "\[BOOT\]" /home/LogFiles/mdp/server-log.log | tail -5
grep -a "\[HEARTBEAT\]" /home/LogFiles/mdp/server-log.log | tail -10
grep -a "persistent=" /home/LogFiles/mdp/server-log.log | tail -3
tail -20 /home/LogFiles/mdp/restarts.log
```

**Expect:** `[BOOT]` with `defaults_applied=[MALLOC_ARENA_MAX=2, ...]` and `persistent=yes`.  
**Red flag:** `[BOOT]` repeating every few minutes = OOM/crash loop.

---

## 2. Page render SLA (`[CLICK->RENDER]`)

```bash
grep -a "\[CLICK->RENDER\]" /home/LogFiles/mdp/server-log.log | tail -30
grep -a "\[CLICK->RENDER\].*SLOW" /home/LogFiles/mdp/server-log.log | tail -20
grep -a "\[CLICK->RENDER\] page=market_data" /home/LogFiles/mdp/server-log.log | tail -10
grep -a "\[CLICK->RENDER\] page=screening" /home/LogFiles/mdp/server-log.log | tail -10
grep -a "\[CLICK->RENDER\] page=newsroom" /home/LogFiles/mdp/server-log.log | tail -10
grep -a "\[CLICK->RENDER\] page=company_filings" /home/LogFiles/mdp/server-log.log | tail -10
grep -a "\[CLICK->RENDER\] page=earnings_calls" /home/LogFiles/mdp/server-log.log | tail -10
grep -a "\[CLICK->RENDER\] page=earnings_calendar" /home/LogFiles/mdp/server-log.log | tail -10
```

**Target:** cold &lt;1s, warm &lt;200ms (current STG audit: median cold 12.7s, warm 1.0s).  
**`SLOW`** = render ≥2s. **`SLOW<-LOW-MEM`** = render ≥2s AND avail &lt;800MB.

---

## 3. Rerun storms (`[RERUN]` / `[RERUN_TRIGGER]`)

```bash
grep -a "\[RERUN\]" /home/LogFiles/mdp/server-log.log | tail -30
grep -a "\[RERUN_TRIGGER\]" /home/LogFiles/mdp/server-log.log | tail -30
grep -a "\[RERUN\] page=screening" /home/LogFiles/mdp/server-log.log | tail -20
grep -a "\[RERUN_TRIGGER\].*screening" /home/LogFiles/mdp/server-log.log | tail -20
grep -a "\[SHOW_RESULTS\]" /home/LogFiles/mdp/server-log.log | tail -10
grep -a "SHOW_RESULTS_RERUN_TOTAL" /home/LogFiles/mdp/server-log.log | tail -10
```

**Interpret:** `page_rerun#=9` on one user action = rerun amplification (screening Show Results).  
**`[RERUN_TRIGGER]`** shows exact `file:line:function` that called `st.rerun()`.

---

## 4. DB timing (`[TIMING] DB_*`)

```bash
grep -a "\[TIMING\] DB_" /home/LogFiles/mdp/server-log.log | tail -40
grep -a "DB_get_companies_rows" /home/LogFiles/mdp/server-log.log | tail -10
grep -a "DB_SELECT_NOKEY" /home/LogFiles/mdp/server-log.log | tail -10
grep -a "DB_SELECT_YF_COMBINED" /home/LogFiles/mdp/server-log.log | tail -10
grep -a "EarningsCalendarRepository" /home/LogFiles/mdp/server-log.log | tail -10
grep -a "FILINGS_LOAD\|FILINGS_PREFETCH\|DB_load_companies" /home/LogFiles/mdp/server-log.log | tail -15
```

**Red flag:** any single `DB_*` line &gt;3000ms on warm cache hit.

---

## 5. Large result sets (`[DATA_VOLUME]`)

```bash
grep -a "\[DATA_VOLUME\]" /home/LogFiles/mdp/server-log.log | tail -20
```

**Fires when:** query returns ≥1000 rows. Investigate wasted columns / missing SQL filters.

---

## 6. Newsroom chunk fetch

```bash
grep -a "NEWSROOM_CHUNK_FETCH" /home/LogFiles/mdp/server-log.log | tail -10
grep -a "AV_ARTICLES_DEDUPE" /home/LogFiles/mdp/server-log.log | tail -10
grep -a "\[BG_PREFETCH\]" /home/LogFiles/mdp/server-log.log | tail -5
```

**Expect:** `chunks=N av_per_chunk=[...] trim_ratio_av=final/raw`.

---

## 7. Market data tabs

```bash
grep -a "\[TIMING\] TAB_" /home/LogFiles/mdp/server-log.log | tail -20
grep -a "PAGE_SUMMARY" /home/LogFiles/mdp/server-log.log | grep market_data | tail -10
grep -a "SEGMENT_DB_FETCH" /home/LogFiles/mdp/server-log.log | tail -10
```

---

## 8. Materialization (`[MAT]`)

```bash
grep -a "\[MAT\]" /home/LogFiles/mdp/server-log.log | tail -30
grep -a "\[MAT\].*MISS" /home/LogFiles/mdp/server-log.log | tail -10
grep -a "\[MAT\].*HIT" /home/LogFiles/mdp/server-log.log | tail -10
```

**Cold restart:** expect `MISS` then `WROTE` once; subsequent loads should be `HIT`.

---

## 9. Memory (`[RAM]` / `[RAM_CENSUS]` / `[MEM_OPT]`)

```bash
grep -a "\[RAM\]" /home/LogFiles/mdp/server-log.log | tail -10
grep -a "\[RAM_CENSUS\]" /home/LogFiles/mdp/server-log.log | tail -5
grep -a "\[MALLOC_TRIM\]" /home/LogFiles/mdp/server-log.log | tail -5
grep -a "\[MEM_OPT\]\|\[MEM_TRIM\]" /home/LogFiles/mdp/server-log.log | tail -10
```

---

## 10. Errors & structured failures

```bash
grep -a "\[STRUCTURED_ERROR\]" /home/LogFiles/mdp/server-log.log | tail -20
grep -a "\[PAGE_LOAD_SLOW\]" /home/LogFiles/mdp/server-log.log | tail -15
grep -a "PERFORMANCE_ALERT" /home/LogFiles/mdp/server-log.log | tail -10
```

---

## 11. User analytics (separate file)

```bash
tail -20 /home/LogFiles/mdp/user_analytics.log
grep '"event":"page_view"' /home/LogFiles/mdp/user_analytics.log | tail -10
```

---

## 12. Correlate one slow request by `rerun_id`

```bash
# Replace R00123 with rerun_id from a slow line
RID=R00123
grep -a "$RID" /home/LogFiles/mdp/server-log.log
```

Log format: `{timestamp} | {level} | {rerun_id} | page={page} | user={email} | {file}:{line}:{func} | {message}`

---

## 13. Env flags (disable subsystems without redeploy)

| Flag | Set to `0` to disable |
|------|------------------------|
| `PAGE_MATERIALIZE` | Disk materialization |
| `WARM_ON_BOOT` | Background cache warmup |
| `SERVER_HEARTBEAT_SECS` | Heartbeat (set `0`) |
| `APP_TIMING` | `[TIMING]` pass-through |

Code defaults (2026-07-02): all ON except explicit Azure App Settings override.

---

## 14. Post-deploy smoke checklist

1. `[BOOT]` once per worker restart  
2. `[MAT][screening_universe] HIT` or `WROTE` within 90s  
3. `/earnings_calls` — `[TIMING] EC_FETCH_COMPANIES` warm &lt;500ms on 2nd load  
4. `/screening` Show Results — `[SHOW_RESULTS_RERUN_TOTAL]` rerun count visible  
5. `/newsroom` filter — `NEWSROOM_CHUNK_FETCH` with chunk breakdown  
6. `/company_filings` — `FILINGS_LOAD` with `file_ok=True`  
7. No spike in `[STRUCTURED_ERROR]` rate

**Full investigation report:** `docs/superpowers/specs/2026-07-02-mdp-performance-investigation-report.md`
