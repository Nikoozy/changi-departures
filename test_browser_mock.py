"""Browser integration tests against the LOCAL mock page (tests/mock_site).

Needs Chrome + Selenium, but no internet.  Run:  python -m pytest -m browser
Optional env vars: CHROME_BINARY, CHROMEDRIVER (only if Selenium Manager can't
find a matching driver on its own).
"""
import csv
import os
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import changi_scraper as s  # noqa: E402

pytestmark = pytest.mark.browser
MOCK = (Path(__file__).parent / "mock_site" / "departures.html").resolve()

# Expected counts for ANY report date, derived by hand from the mock's data table
# (cancelled excluded, SQ106/NH6254 codeshare merged, scheduled-time basis).
EXPECTED = {
    "06:00–08:00": {"MY": 1, "TH": 1, "ID": 1},
    "08:00–10:00": {"TH": 1, "JP": 1},
    "12:00–14:00": {"PH": 1, "AU": 1},
    "14:00–16:00": {"AE": 1},
    "16:00–18:00": {"??": 1},
    "18:00–20:00": {"AU": 1},
    "20:00–22:00": {"HK": 1},
    "22:00–00:00": {"TW": 1, "US": 1},
    "00:00–02:00": {"AU": 2, "KR": 1},
}


def make_cfg(tmp_path, report_date, start="2026-10-10", **kw):
    return s.Config(
        report_date=report_date,
        url=MOCK.as_uri() + f"?terminal=1&start={start}",
        output_dir=tmp_path,
        page_timeout=20,
        chrome_binary=os.environ.get("CHROME_BINARY"),
        chromedriver=os.environ.get("CHROMEDRIVER"),
        **kw,
    )


def read_summary(path):
    out = {}
    with open(path, encoding="utf-8-sig") as fh:
        for row in list(csv.reader(fh))[1:]:
            out.setdefault(row[0], {})[row[1]] = int(row[2])
    return out


@pytest.mark.parametrize("report_date,start", [
    (date(2026, 10, 10), "2026-10-10"),   # main date already shown; next day via calendar
    (date(2026, 9, 30), "2026-10-10"),    # go back a month, then 30 Sep → 1 Oct
    (date(2026, 12, 31), "2026-10-10"),   # forward 2 months, then 31 Dec → 1 Jan 2027
])
def test_full_run_on_mock(tmp_path, report_date, start):
    res = s.run_live(make_cfg(tmp_path, report_date, start))
    assert res.status.startswith("COMPLETE")
    assert read_summary(res.summary_csv) == EXPECTED
    with open(res.detail_csv, encoding="utf-8-sig") as fh:
        det = list(csv.DictReader(fh))
    nums = {r["flight_number"]: r for r in det}
    assert f"TR9{report_date.day:02d}" in nums                      # the selected day really loaded
    assert nums["SQ106"]["codeshare_flight_numbers"] == "NH6254"
    assert nums["MH604"]["counted"] == "no"
    assert nums["TR416"]["airline"] == "Scoot"                     # 'TR416Scoot' format
    nxt = report_date + timedelta(days=1)
    assert {r["scheduled_date"] for r in det if r["time_interval"] == "00:00–02:00"} == {nxt.isoformat()}
    raw_next = (tmp_path / "raw" / f"changi_t1_raw_listing_{nxt}.txt").read_text(encoding="utf-8")
    assert f"TR9{nxt.day:02d}" in raw_next                          # next-day page really loaded
    assert sum(sum(c.values()) for c in res.buckets.values()) == 16


def test_unselectable_date_fails_loudly(tmp_path):
    with pytest.raises(s.ScraperError):
        s.run_live(make_cfg(tmp_path, date(2027, 3, 1)))
    assert any((tmp_path / "debug").glob("*.png"))                   # debug screenshot saved


def test_inspect_mode(tmp_path):
    out = s.run_inspect(make_cfg(tmp_path, date(2026, 10, 10)))
    import json
    rep = json.loads(out.read_text(encoding="utf-8"))
    assert rep["calendar_opened"] and rep["shadowHosts"] >= 1 and rep["rowCount"] > 0
    assert rep["calendar_arrows"]["next"] and rep["calendar_arrows"]["prev"]


FIXTURE = (Path(__file__).parent / "fixtures" / "live_departures_2026-10-21.html").resolve()


def test_real_dom_capture(tmp_path):
    """Static copy of the live page (scripts/CSS stripped, so the hidden mobile layout
    also shows and rows appear twice). Checks the trigger choice and row parsing."""
    cfg = make_cfg(tmp_path, date(2026, 10, 21))
    d = s.build_driver(cfg)
    try:
        d.get(FIXTURE.as_uri())
        trg = s.js(d, "return CT.triggers().map(function(t){ return t.text; });")
        assert trg[0] == "Wed, 21 Oct 2026"          # listing box, not the header search box (Tue, 20 Oct)
        ev = s.date_evidence(d, date(2026, 10, 21))
        assert s.date_confirmed(ev, date(2026, 10, 21))[0]
        assert not s.date_confirmed(s.date_evidence(d, date(2026, 10, 20)), date(2026, 10, 20))[0]
        snap = s.snapshot(d, date(2026, 10, 21))
        assert snap.flights and not snap.unparsed
        flights = s.build_flights(snap.flights, date(2026, 10, 21), s.CountryResolver(), keep_date=date(2026, 10, 21))
        merged, _ = s.dedupe_flights(flights)
        assert len(merged) == 10
        by = {f.flight_number: f for f in merged}
        assert by["EK353"].codeshares == ["QF8353", "TP6356"] and by["ZG54"].country == "JP"
        assert s.js(d, s.JS_COOKIE) == "Continue"
    finally:
        d.quit()
