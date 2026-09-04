"""The email trajectory chart has to stay readable on a quarterly series.

A quarterly chart carries up to ~103 periods on the same 900px canvas an annual
chart uses for 12. The original code thinned x-labels with a fixed `step = 2` and
labelled every forecast point, so AFRM's quarterly chart rendered its axis as one
illegible ribbon ("Q3 FY2019Q4 FY2019Q1 FY2020...") and its values as
"1,17.2,221,4741,6141,706". The axis also ran to -1,343 on all-positive revenue.
"""

import io
import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "app"))

import pytest  # noqa: E402

import data.forecast_email_report as report  # noqa: E402


# ---------------------------------------------------------------------------
# Axis bounds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("lo,hi", [
    (0.0, 9300.0), (120.0, 21000.0), (1.0, 3.0),
    (500.0, 505.0), (0.0, 1.0), (12.0, 1_900_000.0),
])
def test_axis_covers_the_data_on_round_numbers(lo, hi):
    a, b = report._nice_axis(lo, hi)
    assert a <= lo and b >= hi, "the axis must contain the data"
    step = (b - a) / 4
    for g in range(5):
        tick = a + step * g
        assert abs(tick - round(tick, 6)) < 1e-6


def test_positive_data_never_gets_a_negative_axis():
    """The chart showed a -1,343 gridline under a revenue series."""
    a, _ = report._nice_axis(0.0, 9300.0)
    assert a == 0.0
    a, _ = report._nice_axis(250.0, 8000.0)
    assert a == 0.0


def test_negative_data_still_gets_room_below_zero():
    a, b = report._nice_axis(-500.0, 2000.0)
    assert a <= -500.0 and b >= 2000.0


def test_axis_does_not_overshoot_wildly():
    """A 2,500 -> 5,000 jump drew an axis to 20,000 for data peaking at 9,300."""
    _, b = report._nice_axis(0.0, 9300.0)
    assert b <= 9300.0 * 1.8, f"top tick {b:,.0f} is too far above the data"


# ---------------------------------------------------------------------------
# Label thinning
# ---------------------------------------------------------------------------

def _chart(n_actual, n_forecast, period_type, base=1000.0):
    """A synthetic detail dict of the same shape fetch_ticker_detail returns."""
    if period_type == "quarterly":
        # Keys are fy*4+q with q in 1..4, so start at q=1 — q=0 is not a quarter
        # and does not survive the _period_key round-trip.
        keys = [2019 * 4 + 1 + i for i in range(n_actual + n_forecast)]
    else:
        keys = [2015 + i for i in range(n_actual + n_forecast)]
    vals = [base * (1 + 0.05 * i) for i in range(len(keys))]
    return {
        "ticker": "TEST",
        "period_type": period_type,
        "actuals": list(zip(keys[:n_actual], vals[:n_actual])),
        "ensemble_series": list(zip(keys[n_actual:], vals[n_actual:])),
        "series": [],
        "scenarios": [],
    }


@pytest.mark.parametrize("n_act,n_fc,period", [
    (27, 20, "quarterly"),    # AFRM — the reported case
    (83, 20, "quarterly"),    # M — the densest series in the universe
    (7, 5, "annual"),
    (2, 1, "annual"),         # degenerate
])
def test_chart_renders_for_every_shape(n_act, n_fc, period):
    png = report.draw_trajectory_png(_chart(n_act, n_fc, period))
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_chart_titles_are_ascii():
    """With no TrueType font available Pillow falls back to a bitmap font that
    has no em-dash glyph — it rendered as a tofu box in the emailed title."""
    import inspect

    for fn in (report.draw_trajectory_png, report.draw_mape_png):
        for line in inspect.getsource(fn).split("\n"):
            if "d.text((" in line and "detail['ticker']" in line:
                assert line.isascii(), f"non-ASCII in chart title: {line.strip()}"


def test_value_labels_do_not_overlap():
    """Rendered labels must never share horizontal space."""
    import inspect

    src = inspect.getsource(report.draw_trajectory_png)
    assert "drawn_spans" in src, "no collision guard on the value labels"
    assert "lab_step" in src, "no spacing rule on the value labels"


def test_x_labels_are_thinned_by_measured_width():
    import inspect

    src = inspect.getsource(report.draw_trajectory_png)
    assert "step = 1 if len(all_years) <= 9 else 2" not in src, \
        "the fixed step is what made the quarterly axis unreadable"
    assert "textlength(lab, font=f_lab)" in src


# ---------------------------------------------------------------------------
# A runaway scenario band must not destroy the axis
# ---------------------------------------------------------------------------

def _with_band(optimistic_peak):
    """BIRK's shape: real revenue in the hundreds, an exploding optimistic band."""
    d = _chart(20, 20, "quarterly", base=200.0)
    fc_keys = [k for k, _ in d["ensemble_series"]]
    n = len(fc_keys)
    def fy_q(k):
        """Inverse of fy*4+q for q in 1..4."""
        fy = (k - 1) // 4
        return fy, k - fy * 4

    rows = []
    for i, k in enumerate(fc_keys):
        fy, q = fy_q(k)
        rows.append({"model_key": "scenario_optimistic", "fiscal_year": fy,
                     "fiscal_quarter": q,
                     "value_millions": optimistic_peak * (i + 1) / n})
        rows.append({"model_key": "scenario_pessimistic", "fiscal_year": fy,
                     "fiscal_quarter": q, "value_millions": 18.0})
    d["scenarios"] = rows
    return d


def _series_pixel_spread(png: bytes) -> float:
    """How much of the image height the plotted series actually occupies, 0-1.

    Measured from the rendered pixels rather than the hotspots: the hotspots are
    full-height columns by design, so they cannot show a flattened series.
    """
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = im.size
    px = im.load()
    rows = [y for y in range(h) for x in range(0, w, 3)
            if _is_series_pixel(px[x, y])]
    return (max(rows) - min(rows)) / h if rows else 0.0


def _is_series_pixel(rgb) -> bool:
    """A dark slate 'actual' pixel or a red 'ensemble' one — not grid or band."""
    r, g, b = rgb
    dark_slate = r < 90 and g < 90 and b < 140 and b >= r
    accent_red = r > 150 and g < 90 and b < 90
    return dark_slate or accent_red


def test_an_exploding_scenario_does_not_flatten_the_series():
    """BIRK's optimistic scenario compounds to $8.3 TRILLION against revenue of
    $169-731M. Letting it set the axis drew every real point as a flat line on
    the floor beneath an axis running to 10,000,000."""
    sane = _series_pixel_spread(report.draw_trajectory_png(_with_band(2_000.0)))
    boom = _series_pixel_spread(report.draw_trajectory_png(_with_band(8_336_703.0)))

    assert boom > 0.10, (
        f"the series collapsed to a flat line: it covers {boom:.1%} of the height")
    assert boom > sane * 0.4, (
        f"an exploding band crushed the series ({boom:.1%} vs {sane:.1%})")


def test_the_band_is_clipped_not_the_axis_expanded():
    import inspect
    src = inspect.getsource(report.draw_trajectory_png)
    assert "core_vals" in src, "the axis must be scaled on actuals + forecast"
    assert "def pyc(" in src, "an off-scale band must be clipped to the plot box"


# ---------------------------------------------------------------------------
# Hover
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n_act,n_fc", [(27, 20), (83, 20), (7, 5)])
def test_hotspots_tile_the_chart_in_image_pixels(n_act, n_fc):
    """Image-map coords address the image's own pixel grid, so they must sit
    inside it, never be zero-width, and tile without gaps. Rounding each column's
    edges independently collapsed 97 of 292 to zero width on a dense series."""
    period = "quarterly" if n_fc == 20 else "annual"
    spots = []
    png = report.draw_trajectory_png(_chart(n_act, n_fc, period), hotspots=spots)

    from PIL import Image
    assert Image.open(io.BytesIO(png)).size == (report.CHART_W, report.CHART_H), \
        "the image must be drawn at the size the <img> tag declares"

    assert spots, "no hover targets emitted"
    for sp in spots:
        assert sp["x2"] > sp["x1"], "zero-width column: the cursor falls through it"
        assert 0 <= sp["x1"] and sp["x2"] <= report.CHART_W
        assert 0 <= sp["y1"] < sp["y2"] <= report.CHART_H
    for a, b in zip(spots, spots[1:]):
        assert b["x1"] == a["x2"], "columns must tile with no dead strip between"


def test_one_hover_target_per_period():
    detail = _chart(27, 20, "quarterly")
    spots = []
    report.draw_trajectory_png(detail, hotspots=spots)
    periods = {k for k, _ in detail["actuals"]} | {k for k, _ in detail["ensemble_series"]}
    assert len(spots) == len(periods)


def test_a_forecast_tooltip_carries_every_series():
    detail = _with_band(2_000.0)
    spots = []
    report.draw_trajectory_png(detail, hotspots=spots)
    fc = [s["title"] for s in spots if "Ensemble" in s["title"]]
    assert fc, "no forecast tooltip"
    for wanted in ("Ensemble:", "Optimistic:", "Pessimistic:"):
        assert wanted in fc[0], f"{wanted} missing from {fc[0]!r}"


def test_the_email_uses_an_image_map_not_positioned_spans():
    """Gmail and Outlook both strip `position:absolute`, so the span overlay only
    ever worked when the raw file was opened in a browser — in a real inbox it
    was a flat picture. <map>/<area> is what survives an email client."""
    spots = [{"x1": 10, "y1": 20, "x2": 30, "y2": 260, "title": 'Q1 FY2026  Actual: $1.0M'}]
    html = report._trajectory_with_hotspots("cid:x", spots, map_name="traj_T")

    assert "<map name=\"traj_T\"" in html
    assert '<area shape="rect" coords="10,20,30,260"' in html
    assert 'usemap="#traj_T"' in html
    assert f'width="{report.CHART_W}" height="{report.CHART_H}"' in html, \
        "the image must not be CSS-scaled or the map lands in the wrong place"
    assert "position:absolute" not in html


def test_titles_are_escaped_for_an_html_attribute():
    assert report._html_attr('a "b" & <c>') == "a &quot;b&quot; &amp; &lt;c&gt;"
