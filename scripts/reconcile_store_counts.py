"""Choose the final value per ticker-year by reconciling the whole series.

Every layer has a failure mode and none wins outright — measured over 222
rows where both produced a value, L1 and L2 agreed only 27% of the time.
MUSA's XBRL 1,489 is right while its filing parse says 17,621 (a fuel volume);
TXRH's XBRL says 1 while its filing correctly says 697. Preferring either layer
globally is therefore wrong.

What does discriminate is the series. A store fleet moves a few percent a year,
so 1,489 -> 1,503 -> 1,679 is a fleet and 17,621 wedged between them is not.

    1. anchor  years where two independent sources agree
    2. scale   the median anchor, the size this company operates at
    3. resolve every other year by choosing the candidate that fits the scale
    4. report  anything that still cannot be resolved, rather than guessing

Runs entirely on the build output. No model calls, no cost.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

RESULTS = "/tmp/sc_report/build_results.json"
OUT = "/tmp/sc_report/reconciled.json"

# A fleet can move this much year on year before we stop believing it is the
# same quantity. Chains open aggressively; 60% covers even a large acquisition.
SCALE_TOLERANCE = 0.60

# Year-on-year movement a real store fleet can show. Acquisitions and mass
# closures happen, so this is generous — but 6,588 next to 11,718 is not one
# company's fleet in consecutive years, it is two different quantities.
NEIGHBOUR_TOLERANCE = 0.25

# Log-distance from the series median beyond which a value is a different
# quantity, not a different year. 0.40 ~ a factor of 1.5. Deliberately loose:
# dropping a real year costs a gap, keeping a wrong one costs credibility.
# Residual from the fitted trend beyond which a value is a different quantity.
# 0.25 in log space ~ 28%. A fleet follows its own trend closely; McDonald's
# 27,000 sits 43% under a trend running through 40,275, and is franchise text
# rounded to the nearest thousand rather than the restaurant count.
TREND_TOLERANCE = 0.25


def candidates_of(row: Dict[str, Any]) -> Dict[str, Optional[int]]:
    # Rows restored from the accession cache carry only the settled value, not
    # the per-layer columns a fresh extraction produces, so every lookup has to
    # tolerate a missing key. A cached row still offers its own value as a
    # candidate — that is what it is.
    picks = {"l1": row.get("l1_value"), "l2": row.get("l2_value"),
             "l3": row.get("l3_value"), "db": row.get("db_value")}
    # A cached row carries no per-layer columns, only the value the filing
    # produced. Offering it as a candidate ONLY when nothing else exists was
    # wrong: where the DB also had a value, the filing's own reading was left
    # out of the running entirely and the DB won by default. Walmart's exact
    # 11,501 lost to the database's 11,471 that way, which inverts the rule
    # this whole pipeline is built on — the filing is the source of truth.
    if picks["l2"] is None and row.get("value"):
        picks["l2"] = row["value"]
    return picks


def anchor_for(row: Dict[str, Any]) -> Optional[int]:
    """A value two independent sources arrived at separately.

    The DB counts as a source here — it was produced by a different pipeline,
    so DB agreeing with the filing parse is genuine corroboration.
    """
    values = [v for v in candidates_of(row).values() if v]
    if not values:
        return None
    value, count = Counter(values).most_common(1)[0]
    return value if count >= 2 else None


def main() -> None:
    rows = json.load(open(RESULTS))
    by_ticker: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_ticker[row["ticker"]].append(row)

    resolved, unresolved = 0, 0
    changed_from_build = 0
    output: List[Dict[str, Any]] = []

    for ticker, entries in by_ticker.items():
        # Tombstones — filings we read and found nothing in — carry no fiscal
        # year. They belong in the output as evidence of coverage, but they
        # cannot take part in any series reasoning.
        entries = [r for r in entries if r.get("fiscal_year") is not None]
        if not entries:
            continue
        entries.sort(key=lambda r: r["fiscal_year"])
        anchors = {r["fiscal_year"]: anchor_for(r) for r in entries}
        known = [v for v in anchors.values() if v]
        if known:
            scale = statistics.median(known)
        else:
            # No year where two sources agree. Rather than fall back to source
            # priority — which took Asbury's 96,787 (vehicle inventory) over the
            # LLM's correct 110 purely because the filing parse ranks first —
            # take the median of EVERY candidate across the whole series. Wrong
            # readings scatter; the real fleet clusters, so the median lands on
            # it. ABG's candidates run 2, 6, 97, 110, 112, 96787 ... and the
            # median sits with the real dealership count.
            every = [v for r in entries for v in candidates_of(r).values() if v]
            scale = statistics.median(every) if len(every) >= 3 else None

        for row in entries:
            year = row["fiscal_year"]
            picks = candidates_of(row)
            anchor = anchors[year]
            before = row.get("value")

            # THE FILING IS THE SOURCE OF TRUTH. It is where the company itself
            # states the number, so a value read out of the 10-K outranks the
            # XBRL layer and outranks our database — both are derivatives.
            # The only thing that can override it is implausibility, which
            # means our PARSE is wrong rather than the filing: MUSA's 17,621 is
            # a fuel volume, AutoNation's 2,509 an inventory count.
            # With no anchor year there is no scale to sanity-check against, so
            # a filing value cannot be validated at all. Trusting it blindly put
            # AutoNation's 2,509 (vehicle inventory) on screen against a DB
            # value of 239. Unvalidatable values fall through to the guard below.
            filing_value = picks["l2"] or picks["l3"]
            filing_fits = bool(filing_value) and scale is not None and (
                abs(filing_value - scale) / scale <= SCALE_TOLERANCE)

            if filing_value and filing_fits:
                row["final_value"] = filing_value
                agreed = sum(1 for v in picks.values() if v == filing_value)
                row["final_confidence"] = "high" if agreed >= 2 else "medium"
                row["final_basis"] = ("filing (10-K), corroborated" if agreed >= 2
                                      else "filing (10-K)")
            elif anchor:
                row["final_value"] = anchor
                row["final_confidence"] = "high"
                row["final_basis"] = "two sources agree"
            elif scale:
                # Closest candidate to the size this company operates at. Ties
                # go to the filing: WMT FY2020 has the DB's 11,471 and the
                # filing's 11,501 equidistant from scale, and the filing's
                # 'Total retail units' row is the one that is actually true.
                priority = {"l2": 0, "l3": 1, "l1": 2, "db": 3}
                fitting = [(round(abs(v - scale) / scale, 2), priority[name], name, v)
                           for name, v in picks.items() if v]
                fitting = [f for f in fitting if f[0] <= SCALE_TOLERANCE]
                if fitting:
                    _, _, name, value = min(fitting)
                    row["final_value"] = value
                    row["final_confidence"] = "medium"
                    row["final_basis"] = f"fits series ({name})"
                else:
                    row["final_value"] = None
                    row["final_confidence"] = "none"
                    row["final_basis"] = "no candidate fits the series"
            else:
                # Nothing to calibrate against: trust the filing over XBRL,
                # since the filing parse matched the DB 3.4x more often.
                for name in ("l2", "l3", "l1", "db"):
                    if picks[name]:
                        # An unverified single source must never displace what
                        # the portal already shows. AutoNation's filing parse
                        # returns 2,509 against a DB value of 239 — publishing
                        # that would be a regression, not an improvement. Keep
                        # the DB and flag the row for review instead.
                        if name != "db" and picks["db"] and picks[name] != picks["db"]:
                            row["final_value"] = picks["db"]
                            row["final_confidence"] = "low"
                            row["final_basis"] = "kept DB — ours unverified, needs review"
                            row["review_note"] = (
                                f"our {name} value {picks[name]} disagrees with "
                                f"DB {picks['db']} and nothing corroborates it")
                        else:
                            row["final_value"] = picks[name]
                            row["final_confidence"] = "low"
                            row["final_basis"] = f"single source ({name}), unverified"
                        break
                else:
                    row["final_value"] = None
                    row["final_confidence"] = "none"
                    row["final_basis"] = "nothing found"

            if row["final_value"] != before:
                changed_from_build += 1

        # ── Smoothing pass: judge each year against its NEIGHBOURS ───────────
        # The scale test compares against the whole series at once, which is too
        # forgiving at the edges: WMT's US-only 6,588 sits 38% under a ~10,700
        # median and passed a 60% tolerance, and MCD's rounded 27,000 passed the
        # same way. Adjacent years are far more discriminating — a fleet does
        # not move 20% in a year, so a value that disagrees with both of its
        # neighbours is the wrong quantity, and one of the other candidates for
        # that year usually is not.
        chosen = {r["fiscal_year"]: r["final_value"] for r in entries}
        for row in entries:
            year = row["fiscal_year"]
            neighbours = [chosen.get(y) for y in (year - 1, year + 1)]
            neighbours = [v for v in neighbours if v]
            if not neighbours:
                continue
            expected = statistics.median(neighbours)
            current = row["final_value"]
            if current and abs(current - expected) / expected <= NEIGHBOUR_TOLERANCE:
                continue
            options = [(abs(v - expected) / expected, {"l2": 0, "l3": 1, "l1": 2,
                                                       "db": 3}[name], name, v)
                       for name, v in candidates_of(row).items() if v]
            options = [o for o in options if o[0] <= NEIGHBOUR_TOLERANCE]
            if options:
                _, _, name, value = min(options)
                if value != current:
                    row["final_value"] = value
                    row["final_confidence"] = "medium"
                    row["final_basis"] = f"fits neighbouring years ({name})"
                    chosen[year] = value
            elif current:
                # Nothing plausible for this year. A gap is honest; a number
                # that contradicts both neighbours is not.
                row["final_value"] = None
                row["final_confidence"] = "none"
                row["final_basis"] = "no candidate fits neighbouring years"
                chosen[year] = None

        # ── Outlier pass: the majority of the series defines the company ─────
        # Consecutive wrong years agree with each other and so survive a
        # neighbour test: WMT's US-only 6,588/6,718, MCD's rounded
        # 27,000/28,000/29,000, TGT's three 100s. Judging every year against the
        # WHOLE series in log space catches them, because they are a minority
        # cluster at the wrong order of magnitude.
        #
        # Log space matters: a fleet that grows 4,571 -> 6,585 over a decade is
        # a 44% rise in absolute terms but a smooth trend, while 27,000 against
        # a 38,000 median is a different quantity. Ratios, not differences.
        points = [(r["fiscal_year"], r["final_value"]) for r in entries
                  if r["final_value"]]
        if len(points) >= 5:
            import math

            # Theil-Sen: the median of all pairwise slopes. A flat median test
            # cannot tell growth from error — ORLY legitimately runs 4,571 to
            # 6,585 (a factor of 1.44) and would lose its own real endpoints —
            # whereas a trend line tolerates growth and still rejects a step to
            # a different quantity. Median-of-slopes is unmoved by the outliers
            # it is meant to find, which a least-squares fit would not be.
            # ...but median-of-slopes assumes the series is a fleet with a few
            # bad years in it. When the parser has mixed in a SECOND quantity
            # for half the series the median lands between the two groups and
            # rejects both — that is how Albertsons' verified 2,277/2,276/2,271
            # were being withheld alongside the 90/110/90 that displaced them.
            #
            # So fit a line through every pair and keep the best-supported one.
            # Support is judged by the anchors FIRST, not by headcount: on
            # Build-A-Bear the wrong quantity covers more years than the right
            # one, and on AutoNation the vehicle-inventory cluster is both
            # larger and more numerous than the real 240-odd dealerships.
            # A year where two independent sources agree is what identifies
            # which of the two quantities is the fleet.
            # Tried breaking anchorless ties toward whichever line covers the
            # EARLIEST filings, on the theory that a company's history starts at
            # its real size and the misread quantity creeps in later. Carvana
            # improved; Haverty's broke — its early years are the square-footage
            # ones, so the rule handed it 2,152 in place of a correct 121. The
            # premise is simply not true, so headcount stands and Carvana is
            # left to the industry gate and the unsupported report instead.
            def support(slope: float, intercept: float) -> tuple:
                fitted = [(y, v) for y, v in points
                          if abs(math.log(v) - (slope * y + intercept)) <= TREND_TOLERANCE]
                agreed = sum(1 for y, v in fitted if anchors.get(y) == v)
                return (agreed, len(fitted), sum(v for _, v in fitted))

            slope, intercept, best = 0.0, math.log(points[0][1]), (-1, -1, -1)
            for index, (y1, v1) in enumerate(points):
                for y2, v2 in points[index + 1:]:
                    if y2 == y1:
                        continue
                    trial = (math.log(v2) - math.log(v1)) / (y2 - y1)
                    scored = support(trial, math.log(v1) - trial * y1)
                    if scored > best:
                        slope, intercept, best = trial, math.log(v1) - trial * y1, scored

            def predicted(year: int) -> float:
                return slope * year + intercept

            for row in entries:
                value = row["final_value"]
                if not value:
                    continue
                if abs(math.log(value) - predicted(row["fiscal_year"])) <= TREND_TOLERANCE:
                    continue
                centre = predicted(row["fiscal_year"])
                limit = TREND_TOLERANCE
                # Prefer a different candidate for this year that does sit in
                # the company's own range.
                replacement = [
                    (abs(math.log(v) - centre), {"l2": 0, "l3": 1, "l1": 2,
                                                 "db": 3}[name], v)
                    for name, v in candidates_of(row).items()
                    if v and abs(math.log(v) - centre) <= limit]
                if replacement:
                    _, _, value = min(replacement)
                    row["final_value"] = value
                    row["final_confidence"] = "medium"
                    row["final_basis"] = "fits the company's own range"
                else:
                    row["final_value"] = None
                    row["final_confidence"] = "none"
                    row["final_basis"] = ("dropped — outside this company's "
                                          "range, likely a different quantity")

        # A lone year, from a lone source, has nothing to be checked against —
        # every series test above needs neighbours. Asbury survived all of them
        # on 23,709 (a vehicle count) purely because it was the only year left.
        # Corroborated single years are fine; unverified ones are withheld.
        surviving = [r for r in entries if r["final_value"]]
        if len(surviving) == 1 and surviving[0]["final_confidence"] != "high":
            surviving[0]["final_value"] = None
            surviving[0]["final_confidence"] = "none"
            surviving[0]["final_basis"] = ("single unverified year — nothing to "
                                           "corroborate it against")

        for row in entries:
            if row["final_value"]:
                resolved += 1
            else:
                unresolved += 1
            output.append(row)

    json.dump(output, open(OUT, "w"), indent=1, default=str)

    basis = Counter(r["final_basis"].split(" (")[0] for r in output)
    confidence = Counter(r["final_confidence"] for r in output)
    agrees_db = sum(1 for r in output
                    if r["final_value"] and r["final_value"] == r["db_value"])
    print(f"rows            : {len(output)}")
    print(f"resolved        : {resolved}")
    print(f"unresolved      : {unresolved}")
    print(f"changed vs build: {changed_from_build}")
    print(f"agrees with DB  : {agrees_db}")
    print(f"\nbasis     : {dict(basis)}")
    print(f"confidence: {dict(confidence)}")
    print(f"\nWROTE {OUT}")


if __name__ == "__main__":
    main()
