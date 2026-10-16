"""Summary Compensation Table parsing on the three layouts seen in real proxies."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

from data.proxy_pay import accession_from_url, parse_summary_compensation, proxy_url

HEAD = ("<tr><td>Name and Principal Position</td><td></td><td>Year</td><td colspan=2>Salary ($)</td>"
        "<td>Bonus ($)</td><td>Stock Awards ($)</td><td>All Other Compensation ($)</td>"
        "<td>Total ($)</td></tr>")


def table(*rows):
    return f"<p>Summary Compensation Table</p><table>{HEAD}{''.join(rows)}</table>"


def row(label, year, salary, bonus, stock, other, total, lead=""):
    # A spacer cell before the year and "$" in its own cell, as Workiva prints them.
    return (f"<tr><td>{label}</td><td>{lead}</td><td>{year}</td><td>$</td><td>{salary}</td>"
            f"<td>{bonus}</td><td>{stock}</td><td>{other}</td><td>{total}</td></tr>")


def test_name_then_title_rows():
    html = table(row("Doug McMillon", 2026, "1,500,000", "—", "21,051,605", "442,047", "29,240,930"),
                 row("President and CEO", 2025, "1,511,539", "—", "20,375,675", "381,895", "27,408,854"),
                 row("John Furner", 2026, "1,313,000", "—", "22,259,543", "481,188", "27,343,149"))
    rows = parse_summary_compensation(html)
    assert [(r["executive_name"], r["year"]) for r in rows] == [
        ("Doug McMillon", 2026), ("Doug McMillon", 2025), ("John Furner", 2026)]
    assert rows[0]["title"] == "President and CEO"
    assert rows[0]["salary"] == 1_500_000 and rows[0]["bonus"] == 0.0
    assert rows[1]["total_compensation"] == 27_408_854


def test_stacked_years_in_one_cell():
    html = table(row("Hilton H. Schlosberg<br>Vice Chairman", "2025<br><br>2024",
                     "1,250,000<br><br>1,200,000", "—", "10,687,460<br><br>10,498,000",
                     "101,819<br><br>76,955", "19,282,630<br><br>16,868,000"))
    rows = parse_summary_compensation(html)
    assert [(r["year"], r["total_compensation"]) for r in rows] == [
        (2025, 19_282_630), (2024, 16_868_000)]
    assert {r["executive_name"] for r in rows} == {"Hilton H. Schlosberg"}


def test_not_a_compensation_table():
    assert parse_summary_compensation("<table><tr><td>Salary</td><td>Total</td></tr></table>") == []


def test_urls():
    url = proxy_url("https://www.sec.gov/ix?doc=/Archives/edgar/data/1650372/000165037226000044/team.htm")
    assert url == "https://www.sec.gov/Archives/edgar/data/1650372/000165037226000044/team.htm"
    assert accession_from_url(url) == "000165037226000044"


def test_name_and_title_in_one_cell_and_title_only_rows():
    html = table(row("Paul Paradis, Executive Director", 2025, "370,994", "—", "1", "2", "578,478"),
                 row("James A. Sabia, Jr., Executive Vice President", 2025, "1", "—", "1", "2", "9"),
                 row("Chief Financial Officer", 2025, "1", "—", "1", "2", "9"))
    rows = parse_summary_compensation(html)
    assert [(r["executive_name"], r["title"]) for r in rows] == [
        ("Paul Paradis", "Executive Director"),
        ("James A. Sabia, Jr", "Executive Vice President")]
