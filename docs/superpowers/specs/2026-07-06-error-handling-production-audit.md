# Error-handling production-readiness audit

**Date:** 2026-07-06
**Author:** Claude (Opus 4.8), driven by Mohd Saeed Afri
**Goal:** verify no error is silently swallowed, every real failure is logged debuggably, and no
error can crash a page into a blank screen. Audited `app/` (1344 `except` blocks).

---

## Backbone — verified GOOD (production-grade)

- **Global safety net** (`main.py:642`): `pg.run()` is wrapped; on error it logs the **full
  traceback** (`log_exception` → `logger.exception`) and **re-raises**. It fired **0×** in the last
  session (no false FATALs).
- **Control-flow is not eaten:** in this Streamlit build `RerunException`/`StopException` are
  **BaseException-only (not `Exception`)** — verified — so `except Exception:` blocks never swallow
  `st.rerun()`/`st.stop()`/`st.switch_page()`. Only **bare `except:`** could, which is why they were
  all removed (below).
- **Structured error logging is strong:** `log_structured_error(exc, page=, component=, operation=,
  context=)` records exception type, message, deepest origin frame (`file:func:line`), a compact
  traceback, and mirrors into the per-user analytics feed. 620 call sites use it.
- **Page-level safety nets exist broadly:** newsroom, earnings_calls, earnings_calendar, home,
  forecasting, logs, live_earnings_transcript, screening, company_filings, and market_data all wrap
  their entry in `try/except → log + friendly message`.

## Gaps FOUND and FIXED

| # | File / line | Category | Severity | Fix |
|---|---|---|---|---|
| 1 | `access_management.py` DB-health probe | silent-swallow driving an **access-control** decision | HIGH | now `log_structured_error(...)` — a transient DB failure that flips a user to the hardcoded-admin fallback is traced |
| 2 | `access_management.py` (`main()` bare call) | no page safety net | HIGH | wrapped in `try/except → log_structured_error + st.error` (was the only page without one) |
| 3 | `repository.py` ×5 (currency/json fallbacks) | bare `except:` | LOW | → `except Exception:` (no longer eats KeyboardInterrupt/SystemExit) |
| 4 | `market_data.py` `_ff()` float helper | bare `except:` | LOW | → `except Exception:` |
| 5 | `company_filings.py` ×3 (cache clears) | bare `except:` | LOW | → `except Exception:` |
| 6 | `main.py` `_prewarm_screening_tables` | silent background swallow | MEDIUM | → `log_exception(...)` so a broken warmup (missing table / bad query) is visible |
| 7 | `main.py` `_warm_non_sec_cache` | silent background swallow | MEDIUM | → `log_exception(...)` |

**Result:** bare `except:` count **9 → 0**; both access-control and startup-warmup failures now leave
a debuggable trace. All changed files `py_compile` clean.

## Round 2 — "fix everything" pass (direct stdlib-logging → structured log)

Every **direct** stdlib-`logging` call on an error/warning path now routes through `server_logger`:

| File | Was | Now |
|---|---|---|
| `utils/local_storage.py` ×4 | `logger.error(…)` | `log_error(…)` |
| `utils/local_storage_manager.py` | `logger.warning(…)` | `log_error(…)` |
| `data/repository.py` (LLM fallback) | `logging.getLogger().warning(…)` | `log_structured_error(e, …)` |
| `utils/filing_units.py` | `logging.getLogger().warning(…)` | `log_structured_error(e, …)` |

Verified: the only remaining `logging.getLogger().(warning/error)` hits are (a) `server_logger.py`'s
**own internals** (it *is* built on Python logging) and (b) the **`except ImportError:` fallback
stubs** — both correct and intentionally kept (removing the fallbacks would be *worse*: they are the
last-resort logger if `server_logger` itself fails to import).

Also audited the security-critical files the rate-limited agent never reached
(`auth_manager.py`, `database.py`, `access_control.py`, `auth_environment.py`): their no-log
`except` blocks are **safe best-effort with safe defaults** — `_is_secure_context()` defaults to
"secure", the id-token file check fails **closed**, `health_check()` returns `False` (its contract),
cookie/temp-file probes clean up quietly. None hide a real error the way access_management did.
`access_control.py` had **zero** unlogged excepts.

## Intentionally NOT changed (and why — these are NOT leaks)

- **`except ImportError:` fallback stubs** across ~14 modules — defensive; only run if `server_logger`
  fails to import. Keep.
- **Perf diagnostics** (`_perf_logger`, `models.py [FQ_CALC_SLOW]` print) — not error handling; routing
  them to ERROR would spam. Out of scope.
- **INFO-level diagnostics** (`repository.py` `[NEWSROOM_TIMING]/[NEWSROOM_QUERY]`) — not errors.
- **Bootstrap prints** (`main.py` FATAL import failures, `server_logger.py` self-error) — correct;
  they run *before/within* the logger and must not depend on it.
- **~288 best-effort `except … : pass`** (optional cache writes, cleanup, cookie/JS probes, telemetry)
  — sampled; a failure is safe to ignore and logging would be noise. The ones that actually mattered
  (access-control decision, startup warmups, the direct-logging error paths) are all fixed.

**Final state:** bare `except:` = **0**; direct stdlib error/warning logging = **0** (excl. logger
internals + defensive fallbacks); all changed files `py_compile` clean.

## Note (separately tracked, not error-handling)

- STG runs on the **default `SECRET_KEY`** (`config.py:249` boot warning) — set the env var.
- The audit's 4-agent verification pass was cut short by a session rate-limit; this pass was completed
  inline. A full re-run of the multi-agent audit (auth-entry, data-layer, utils deep dives) can be
  done after the limit resets for extra coverage.
