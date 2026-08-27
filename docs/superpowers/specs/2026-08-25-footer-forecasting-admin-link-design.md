# Footer "Forecasting Admin" link — correct page + correct gate

**Date:** 2026-08-25
**Scope:** `app/components/navigation.py`, `tests/test_footer_forecast_admin_link.py`

## Problem

The Quick Links footer linked to `/forecasting_admin` and gated the link on
`check_access("forecasting_admin", user_email, "allow")`.

`pages/forecasting_admin.py` **is not registered** in `main.py` — the page list at
`app/main.py:195-206` ends at `("pages/forecasting.py", "Forecasting", "forecasting", False)`
and never includes it. Streamlit only serves registered pages, so the footer link
pointed at a URL that 404s.

The admin capability itself lives on the **Forecasting** page: `forecasting.py:2979`
(`_get_refresh_permissions`) decides who may run the models with
`check_access("forecasting", user_email, required_permission="edit")`.

## Change

| | before | after |
|---|---|---|
| href | `/forecasting_admin` (404) | `/forecasting` |
| ACL page | `forecasting_admin` | `forecasting` |
| permission | `allow` (4) | `edit` (3) |
| cache key | `fc_admin_access_{email}` | `forecast_can_run_{email}` |

The gate now asks the same question the page asks, so link and controls cannot
drift. The cache key was renamed because it no longer mirrors
`forecasting_admin.py`'s key — reusing that name would collide with a cached
answer to a *different* question.

Failure to reach the DB is still not cached (it would hide the link from a real
admin for the whole session), and the answer is memoised per session because the
footer renders on every page (~300ms uncached query on Azure).

## Who sees the link — measured against the staging ACL

| user | `forecasting`/`edit` (new) | `forecasting_admin`/`allow` (old) |
|---|---|---|
| mohdsaeedafri@coresight.com | True | True |
| shashankgupta@coresight.com | True | True |
| nidhishamohandas@coresight.com | True | True |
| test_edit@coresight.com | True | False |
| test_viewonly@coresight.com | False | False |
| dataautomation@coresight.com | False | False |

No real user loses access; `view_only` and ungranted users still don't get it.
(`coreiq_page_access_control` holds 3 `forecasting` rows, all created by
`test_harness`, and 2 real `forecasting_admin` rows — the admins above are
admitted by the admin/super_user bypass inside `check_access`, not by a row.)

## Testing

`tests/test_footer_forecast_admin_link.py` — 11 tests, all passing. Covers: who is
admitted, that the gate asks about `forecasting`/`edit`, one query per session, a
failed lookup is not cached, and that the anchor renders (and is absent) in the
real footer HTML.

```
.venv/bin/python -m pytest tests/test_footer_forecast_admin_link.py -q
11 passed
```

Footer renders correctly on `/home` and `/forecasting` at `http://localhost:8501`.
The link does not appear in a headless run because that session is anonymous
(`user=-`) — the local launcher only populates the user on an authenticated path,
and the pre-existing Access Management link is equally absent there. Per-user
admission is covered by the ACL table above, taken from the live staging DB.

## Not changed

`pages/forecasting_admin.py` itself is left alone — it is unregistered, unreachable
dead weight, but removing it is out of scope for this correction.
