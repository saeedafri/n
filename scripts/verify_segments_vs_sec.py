"""Check the Segments tab against the company's own SEC filings, both directions.

    .venv/bin/python scripts/verify_segments_vs_sec.py WMT MDLZ ...     # some companies
    .venv/bin/python scripts/verify_segments_vs_sec.py --all             # every company with segment rows
    options: --out results.jsonl  --jobs 6  --tenk 16  --tenq 40  --extracts DIR

For each company, one at a time: its 10-Ks and 10-Qs are downloaded into a private
temporary folder (EDGAR_LOCAL_DATA_DIR), every segment fact is extracted, the folder
is deleted, and the app's own builders produce the annual and quarterly tables from
the database. Nothing is kept on disk but one result line per company.

Shown -> SEC   every number the tab shows must be a fact SEC filed for that
               segment and period, from the newest filing that reports it.
SEC -> shown   every segment fact the newest filing reports for the five metrics
               (on the segment axis, or alone on the geographic axis) must be shown.

Each result line: {"ticker", "annual": {...}, "quarterly": {...}} with counts and the
cells that did not match, so a new company can be checked the day it is added.
"""
import argparse
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))

SEGMENT_AXIS = "StatementBusinessSegmentsAxis"
GEO_AXES = ("StatementGeographicalAxis",)
OTHER_BUSINESS_AXES = ("ProductOrServiceAxis", "ContractWithCustomerSalesChannelAxis",
                       "StatementOperatingActivitiesSegmentAxis")
WRAPPERS = {"OperatingSegmentsMember", "SegmentReportingSegmentMember", "ReportableSegmentsMember"}
# A single-segment company's one member is the company itself: its Total row.
WHOLE_COMPANY = {"ReportableSegmentMember", "ReportableSegmentsMember", "OperatingSegmentsMember"}
TOLERANCE = 0.0015          # $1,500 on values in millions: one rounding unit of a filing


# ── SEC side (runs in a child process with its own EDGAR folder) ──────────────

def extract(ticker, tenk, tenq):
    """Every currency fact with a dimension in the latest filings, as plain rows."""
    from edgar import Company, set_identity
    set_identity(os.getenv("EDGAR_IDENTITY") or "Coresight Research data@coresight.com")
    rows = []
    company = Company(ticker)
    for form, count in (("10-K", tenk), ("10-Q", tenq)):
        for filing in list(company.get_filings(form=form, amendments=False))[:count]:
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    xbrl = filing.xbrl()
            except Exception:
                continue
            if not xbrl:
                continue
            units = getattr(xbrl, "units", {}) or {}
            for fact in xbrl.query().with_dimensions().execute():
                unit = units.get(fact.get("unit_ref")) or {}
                if (fact.get("numeric_value") is None or unit.get("type") != "simple"
                        or not str(unit.get("measure", "")).startswith("iso4217:")):
                    continue
                dims = {k[4:]: v for k, v in fact.items() if k.startswith("dim_")}
                if dims:
                    rows.append({"form": form, "filed": str(filing.filing_date),
                                 "concept": fact.get("concept"), "label": fact.get("original_label") or fact.get("label"),
                                 "dims": dims, "member": fact.get("dimension_member_label"),
                                 "value": fact["numeric_value"] / 1e6,
                                 "start": fact.get("period_start"), "end": fact.get("period_end"),
                                 "instant": fact.get("period_instant")})
    return rows


def sec_facts(ticker, tenk, tenq, extracts=None):
    """Download, extract and delete in a child process; the parent never touches EDGAR.
    With `extracts`, the small extract (never the filings) is kept there and reused,
    so a re-check after a code change needs no download."""
    import gzip
    saved = Path(extracts) / f"{ticker}.json.gz" if extracts else None
    if saved and saved.exists():
        return json.loads(gzip.open(saved, "rt").read())
    rows = download_and_extract(ticker, tenk, tenq)
    if saved:
        saved.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(saved, "wt") as handle:
            json.dump(rows, handle, default=str)
    return rows


def download_and_extract(ticker, tenk, tenq):
    folder = tempfile.mkdtemp(prefix=f"sec_{ticker}_")
    try:
        env = dict(os.environ, EDGAR_LOCAL_DATA_DIR=folder)
        done = subprocess.run([sys.executable, __file__, "--extract", ticker, str(tenk), str(tenq)],
                              env=env, capture_output=True, text=True, timeout=3600)
        if done.returncode != 0:
            raise RuntimeError(done.stderr.strip().splitlines()[-1] if done.stderr.strip() else "extract failed")
        return json.loads(done.stdout)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


# ── classification shared with the app ───────────────────────────────────────

def day(text):
    try:
        return date.fromisoformat(str(text).strip()[:10]) if text else None
    except ValueError:
        return None


def fiscal_year(end):
    return end.year - 1 if end.month == 1 and end.day <= 7 else end.year


def words(qname):
    local = re.sub(r"Member$", "", qname.split(":")[-1])
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", local)


def metric_of(row):
    from data.repository import SegmentDataRepository as repo
    from utils.constants import SEGMENT_METRIC_GROUPS
    for name, cfg in SEGMENT_METRIC_GROUPS.items():
        if repo._matches_metric(row["label"] or "", cfg, row["concept"] or ""):
            return name
    return None


def placement(dims):
    """("business"|"geo", member qname) for a fact filed for one whole segment or
    geography, else None (slices, product lines, other breakdowns)."""
    core = {k.split("_", 1)[-1]: v for k, v in dims.items() if "ConcentrationRisk" not in k}
    if core.get("ConsolidationItemsAxis", "").split(":")[-1] in WRAPPERS:
        core.pop("ConsolidationItemsAxis")
    if set(core) == {SEGMENT_AXIS}:
        if core[SEGMENT_AXIS].split(":")[-1] in WHOLE_COMPANY:
            return None
        return "business", core[SEGMENT_AXIS]
    if set(core) in ({SEGMENT_AXIS, "SubsegmentsAxis"}, {"SubsegmentsAxis"}):
        return "business-sub", core["SubsegmentsAxis"]      # a sub-segment the tab can list
    if len(core) == 1 and next(iter(core)) in OTHER_BUSINESS_AXES:
        return "business-other", next(iter(core.values()))  # product lines, channels: listed when a company has no segments
    if len(core) == 1 and next(iter(core)) in GEO_AXES:
        return "geo", next(iter(core.values()))
    return None


def names_for(row, qname):
    """Every spelling the app could show for this member: the filed label, the
    element name, and the business team's name for either."""
    from data.repository import SegmentDataRepository as repo
    from data.segment_aliases import canonicalize_geo_label
    raw = [row["member"] or "", words(qname)]
    out = set()
    for text in raw:
        if not text or repo.is_wrapper_label(text):
            continue
        shown = repo._strip_member_artifact(repo._title_case_member(text))
        out.update({shown, canonicalize_geo_label(shown)})
    return {key(n) for n in out if n}


def display_name(row, qname):
    """The filed label ("CHINA" for country:CN), or the element's words when the
    label is only the wrapper's ("Operating Segments")."""
    from data.repository import SegmentDataRepository as repo
    label = row["member"] or ""
    return words(qname) if not label or repo.is_wrapper_label(label) else label


def key(name):
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# ── comparison ────────────────────────────────────────────────────────────────

def newest_facts(rows, view):
    """(placement, metric, column) → {member qname: [(value, names)]} from the newest
    filing that reports each member for that column."""
    found = defaultdict(lambda: defaultdict(list))
    for row in rows:
        if view == "annual" and row["form"] != "10-K" or view == "quarterly" and row["form"] != "10-Q":
            continue
        place = placement(row["dims"])
        metric = place and metric_of(row)
        if not metric:
            continue
        start, end, instant = day(row["start"]), day(row["end"]), day(row["instant"])
        if view == "annual":
            if start and end and 340 <= (end - start).days <= 380:
                column = fiscal_year(end)
            elif instant:
                column = fiscal_year(instant)       # balances at the year end
            else:
                continue
        else:
            if not (start and end and 80 <= (end - start).days <= 100):
                continue
            column = end
        found[(place[0], metric, column)][place[1]].append((row["filed"], row["value"], row, place[1]))
    newest = {}
    for slot, members in found.items():
        # one filing per column, as the tab shows it: the newest that reports it
        latest = max(f[0] for facts in members.values() for f in facts)
        newest[slot] = {qname: [(v, names_for(r, q), display_name(r, q)) for filed, v, r, q in facts if filed == latest]
                        for qname, facts in members.items() if any(f[0] == latest for f in facts)}
    return newest


def app_columns(table, view):
    """(section, metric, column) → {shown name: value} from the app's own tables."""
    cells = {}
    for section, part in (("business", "business_segments"), ("geo", "geo_segments")):
        for metric, members in (table.get(part) or {}).items():
            for member, by_period in members.items():
                for period, value in by_period.items():
                    if value is None:
                        continue
                    column = period if view == "annual" else table["period_dates"][period]
                    cells.setdefault((section, metric, column), {})[member] = value
    return cells


def near(a, b):
    return abs(a - b) <= max(TOLERANCE, abs(b) * 2e-6)


def why_not_shown(section, name, values, app):
    """Why a filed segment fact has no cell, by the tab's own documented rules:
    sec_shown_as_parts    the tab lists its parts, and they add up to it
    sec_withheld_overlap  a place containing, or inside, a place the column shows
                          (one geographic breakdown per column, never both)
    sec_withheld_aggregate the sum of other shown members (Abbott's "Non-US")
    sec_excluded_by_rule  not a segment: corporate, reconciling, eliminations
    sec_merged_by_name_map the business team's map gives it the name of a row the
                          column already shows ("Other Americas" -> Americas)
    sec_missing           none of these; split by the caller into
                          missing_not_in_db (the database lacks the value: data team)
                          missing_app_gap   (the database has it, the tab does not)"""
    from itertools import combinations
    from data.repository import SegmentDataRepository as repo
    from data.segment_aliases import canonicalize_geo_label
    shown = list(app.values())
    sums = lambda size: (sum(c) for c in combinations(shown[:20], size))
    if any(near(total, v) or abs(total - v) <= 0.6 for v in values
           for size in range(2, min(6, len(shown)) + 1) for total in sums(size)):
        return "sec_shown_as_parts"
    everything = sum(shown)
    if len(shown) >= 3 and any(abs(everything - total - v) <= max(0.6, abs(v) * 0.001) for v in values
                               for size in range(0, 4) for total in ([0] if size == 0 else sums(size))):
        return "sec_withheld_aggregate"
    row = {"dimension": "us-gaap:StatementBusinessSegmentsAxis" if section == "business"
           else "srt:StatementGeographicalAxis", "dimension_member_label": name, "dimension_label": name,
           "full_dimension_label": ("Segments: " if section == "business" else "Geographical: ") + name}
    if repo._classify_member(row, set(), frozenset({"us-gaap:StatementBusinessSegmentsAxis"})) is None:
        return "sec_excluded_by_rule"
    mapped = canonicalize_geo_label(repo._title_case_member(name))
    if section == "geo" and mapped != repo._title_case_member(name) and mapped in app:
        return "sec_merged_by_name_map"      # the business map names it like a row already shown
    if section == "geo":
        place = repo._geo_node(canonicalize_geo_label(repo._title_case_member(name)))
        if place and any(place in repo._geo_ancestors(m) or repo._geo_node(m) in repo._geo_ancestors(
                canonicalize_geo_label(repo._title_case_member(name))) for m in app):
            return "sec_withheld_overlap"
    return "sec_missing"


def database_values(ticker):
    """(fiscal year | quarter end) -> segment values coreiq_filing_metrics_v5 holds,
    to tell a fact the database lacks from one the tab leaves out."""
    from data.repository import SegmentDataRepository as repo
    annual, quarterly = defaultdict(set), defaultdict(set)
    for row in repo._fetch_all_db_rows(ticker):
        when = row.get("period_end") or row.get("period_instant")
        if when and row.get("numeric_value") is not None and not row.get("_is_ndim"):
            annual[fiscal_year(when)].add(round(row["numeric_value"] / 1e6, 1))
    for row in repo._fetch_all_db_rows_quarterly(ticker, date(2018, 1, 1), date(2026, 12, 31)):
        when = row.get("period_end")
        if when and row.get("numeric_value") is not None and not row.get("_is_ndim"):
            quarterly[when].add(round(row["numeric_value"] / 1e6, 1))
    return {"annual": annual, "quarterly": quarterly}


def compare(table, rows, view, held=None):
    sec = newest_facts(rows, view)
    shown = app_columns(table, view) if table else {}
    counts, issues = Counter(), []

    def sec_slot(section, metric, column):
        if view == "annual":
            return sec.get((section, metric, column))
        for shift in range(-3, 4):          # quarter ends drift a few days between filings
            hit = sec.get((section, metric, column + timedelta(days=shift)))
            if hit:
                return hit
        return None

    us_only = {column for (section, metric, column), members in shown.items()
               if section == "geo" and metric == "Revenues" and list(members) == ["United States"]
               and table and near(members["United States"], (table.get("revenue_totals") or {}).get(
                   column if view == "annual" else None) or -1)}
    for (section, metric, column), members in shown.items():
        if section == "geo" and metric == "Revenues" and column in us_only:
            counts["shown_us_only_inferred"] += 1   # no geographic note: all revenue shown as US
            continue
        facts = sec_slot(section, metric, column)
        if section == "business":                        # the tab may list sub-segments or product lines
            facts = {**(sec_slot("business-other", metric, column) or {}),
                     **(sec_slot("business-sub", metric, column) or {}), **(facts or {})} or None
        if facts is None and section == "geo":           # places filed as segments
            facts = sec_slot("business", metric, column)
        for member, value in members.items():
            if not facts:
                counts["shown_no_sec_column"] += 1
                issues.append(["shown_no_sec_column", section, metric, str(column), member, value, None])
                continue
            # names pooled per member element: Ingredion's wrapped fact carries only the
            # element's words, its plain twin the filed label "F&II - LATAM"
            mine = [v for options in facts.values()
                    if key(member) in set().union(*(n for _, n, _ in options)) for v, _, _ in options]
            if any(near(value, v) for v in mine):
                counts["shown_ok"] += 1
            elif mine:
                # The newest filing restated it. If the database has that figure the
                # tab should show it; if not, the database lacks the newer filing.
                newer_held = held is not None and any(
                    round(m, 1) in (held["annual"].get(column, set()) if view == "annual" else
                                    set().union(*(v for d, v in held["quarterly"].items()
                                                  if abs((d - column).days) <= 3)) or set())
                    for m in mine)
                kind = "shown_superseded_app" if newer_held else "shown_superseded_not_in_db"
                counts[kind] += 1
                issues.append([kind, section, metric, str(column), member, value, mine[0]])
            elif any(near(value, v) for options in facts.values() for v, _, _ in options):
                counts["shown_other_name"] += 1
                issues.append(["shown_other_name", section, metric, str(column), member, value, None])
            else:
                counts["shown_not_in_sec"] += 1
                issues.append(["shown_not_in_sec", section, metric, str(column), member, value, None])

    for (section, metric, column), members in sec.items():
        if view == "annual" and table and column not in (table.get("years") or []):
            continue
        if view == "quarterly" and column < date(2018, 1, 1):      # the tab's quarterly window
            continue
        app = shown.get(("business" if section.startswith("business") else section, metric, column), {}) or {}
        if section.startswith("business"):                # a place filed as a segment may be shown as geography
            app = {**shown.get(("geo", metric, column), {}), **app}
        for qname, options in members.items():
            names = set().union(*(n for _, n, _ in options))
            values = [v for v, _, _ in options]
            shown_as = options[0][2]
            if any(key(m) in names and any(near(v, x) for x in values) for m, v in app.items()):
                counts["sec_shown"] += 1
            elif any(any(near(v, x) for x in values) for v in app.values()):
                counts["sec_shown_other_name"] += 1
            else:
                reason = ("sec_subsegment_parent_shown" if section == "business-sub"
                          else "sec_other_breakdown" if section == "business-other"   # the segment note is shown instead
                          else why_not_shown(section, shown_as, values, app))
                if reason == "sec_missing" and held is not None:
                    value = round(values[0], 1)
                    if view == "annual":
                        inside = value in held["annual"].get(column, set())
                    else:
                        inside = any(value in vals for when, vals in held["quarterly"].items()
                                     if abs((when - column).days) <= 3)
                    reason = "missing_app_gap" if inside else "missing_not_in_db"
                counts[reason] += 1
                issues.append([reason, section, metric, str(column), shown_as, None, values[0]])
    return {"counts": dict(counts), "issues": issues}


def check(ticker, tenk, tenq, extracts=None):
    from data.repository import SegmentDataRepository as repo
    rows = sec_facts(ticker, tenk, tenq, extracts)
    with contextlib.redirect_stdout(io.StringIO()):
        # what the tab shows: the database tables, or EDGAR's for a company not loaded yet
        annual = (repo._build_segment_tables_from_db(ticker, date(2012, 1, 1), date(2026, 12, 31))
                  or getattr(repo._fetch_from_edgartools, "__wrapped__", repo._fetch_from_edgartools)(ticker))
        quarterly = repo._build_segment_tables_quarterly(ticker, date(2018, 1, 1), date(2026, 12, 31))
    held = database_values(ticker)
    return {"ticker": ticker, "annual": compare(annual, rows, "annual", held),
            "quarterly": compare(quarterly, rows, "quarterly", held)}


def all_tickers():
    """Every company the app covers: those with filing rows and those in the
    company master that the Segments tab serves straight from EDGAR."""
    from core.database import db_manager
    rows = db_manager.execute_query_readonly(
        "SELECT DISTINCT ticker FROM coreiq_filing_metrics_v5 WHERE is_dimensioned = 1 AND doc_type = '10-K'", {})
    master = db_manager.execute_query_readonly(
        "SELECT ticker FROM coreiq_companies WHERE ticker IS NOT NULL AND TRIM(ticker) <> ''", {})
    return sorted({r["ticker"] for r in rows} | {r["ticker"] for r in master})


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--extract":
        print(json.dumps(extract(sys.argv[2], int(sys.argv[3]), int(sys.argv[4])), default=str))
        return
    from dotenv import load_dotenv
    load_dotenv(REPO / ".env")
    # The app's EDGAR fallback runs in this process too: keep it out of ~/.edgar.
    own_folder = None
    if not os.getenv("EDGAR_LOCAL_DATA_DIR"):
        own_folder = tempfile.mkdtemp(prefix="sec_check_")
        os.environ["EDGAR_LOCAL_DATA_DIR"] = own_folder
    try:
        run()
    finally:
        if own_folder:
            shutil.rmtree(own_folder, ignore_errors=True)


def run():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tickers", nargs="*")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--out", default="-")
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--tenk", type=int, default=16)      # FY2012 onwards, the tab's annual window
    parser.add_argument("--tenq", type=int, default=40)      # 2018 onwards, the tab's quarterly window
    parser.add_argument("--extracts", help="keep the small per-company extracts here for re-checks")
    args = parser.parse_args()
    tickers = all_tickers() if args.all else [t.upper() for t in args.tickers]
    done = set()
    if args.out != "-" and os.path.exists(args.out):                # resume
        done = {json.loads(line)["ticker"] for line in open(args.out) if line.strip()}
    out = sys.stdout if args.out == "-" else open(args.out, "a")
    from concurrent.futures import ThreadPoolExecutor

    def one(ticker):
        try:
            return check(ticker, args.tenk, args.tenq, args.extracts)
        except Exception as error:
            return {"ticker": ticker, "error": repr(error)[:300]}

    with ThreadPoolExecutor(args.jobs) as pool:
        for result in pool.map(one, [t for t in tickers if t not in done]):
            out.write(json.dumps(result, default=str) + "\n")
            out.flush()
            summary = {v: Counter(result.get(v, {}).get("counts", {})) for v in ("annual", "quarterly")}
            print(result["ticker"], result.get("error") or {v: dict(c) for v, c in summary.items()},
                  file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
