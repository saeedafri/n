"""What would a batch actually PUBLISH, once the series tests run?

batch_check.py prints the parser's raw proposals, which makes the free layer
look worse than it is: the publish path already withholds any value that does
not fit its own company's trend. This applies that same Theil-Sen residual test
to a batch so the two numbers can be compared honestly.

    .venv/bin/python scripts/batch_survival.py 2 3
"""

from __future__ import annotations

import json
import math
import statistics
import sys
from collections import defaultdict

# Same threshold the publish path uses: 0.25 in log space, about 28%.
MAX_RESIDUAL = 0.25


def theil_sen(series: dict) -> dict:
    """The current publish rule: one median-slope line through everything."""
    if len(series) < 4:
        return series
    years = sorted(series)
    slopes = [(math.log(series[b]) - math.log(series[a])) / (b - a)
              for i, a in enumerate(years) for b in years[i + 1:]
              if series[a] > 0 and series[b] > 0]
    if not slopes:
        return series
    slope = statistics.median(slopes)
    intercept = statistics.median(
        [math.log(series[y]) - slope * y for y in years if series[y] > 0])
    return {y: v for y, v in series.items()
            if v > 0 and abs(math.log(v) - (slope * y + intercept)) <= MAX_RESIDUAL}


def survives(series: dict) -> dict:
    """Keep the largest set of years that can be ONE quantity.

    Fitting a single median-slope line assumes the series is a fleet with a few
    bad years in it. When the parser has mixed in a second quantity for half the
    series — Build-A-Bear's 361/373/372 stores against 71/72/68 of something
    else — the median lands between the two groups and rejects both, which is
    how correct values like Albertsons' verified 2,277 were being withheld.

    So: fit a line through every PAIR of years, count how many other years sit
    on it, and keep the best-supported line. The dominant quantity wins and the
    intruder is dropped, rather than both being lost.
    """
    years = [y for y in sorted(series) if series[y] > 0]
    if len(years) < 4:
        return series

    def inliers(slope: float, intercept: float) -> dict:
        return {y: series[y] for y in years
                if abs(math.log(series[y]) - (slope * y + intercept)) <= MAX_RESIDUAL}

    best = {}
    for index, first in enumerate(years):
        for second in years[index + 1:]:
            slope = (math.log(series[second]) - math.log(series[first])) / (second - first)
            fitted = inliers(slope, math.log(series[first]) - slope * first)
            # More years wins; on a tie prefer the larger fleet, because the
            # intruding quantity is usually a subset or an activity count.
            if (len(fitted), sum(fitted.values())) > (len(best), sum(best.values())):
                best = fitted
    return best if len(best) >= 3 else series


def main() -> None:
    for batch in sys.argv[1:] or ["1", "2", "3"]:
        rows = json.load(open(f"/tmp/sc_report/batch_{batch}.json"))
        per_ticker = defaultdict(dict)
        for row in rows:
            if row.get("value") and row.get("fy"):
                per_ticker[row["ticker"]][int(row["fy"])] = int(row["value"])

        proposed = sum(len(s) for s in per_ticker.values())
        old_kept = new_kept = 0
        print(f"\n══ batch {batch} ══")
        for ticker, series in sorted(per_ticker.items()):
            old, new = theil_sen(series), survives(series)
            old_kept, new_kept = old_kept + len(old), new_kept + len(new)
            dropped = {y: v for y, v in series.items() if y not in new}
            rescued = {y: v for y, v in new.items() if y not in old}
            if dropped or rescued:
                print(f"  {ticker:6} withheld {len(dropped)}: "
                      f"{ {y: f'{v:,}' for y, v in sorted(dropped.items())} }")
                if rescued:
                    print(f"  {'':6}   RESCUED {len(rescued)}: "
                          f"{ {y: f'{v:,}' for y, v in sorted(rescued.items())} }")
        print(f"  proposed {proposed} → published {new_kept} "
              f"(single-line rule published {old_kept})")


if __name__ == "__main__":
    main()
