"""Offline unit tests — no browser, no network.  Run:  python -m pytest -m "not browser and not live" """
import csv
import sys
from collections import Counter
from datetime import date, datetime, time as dtime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import changi_scraper as s  # noqa: E402

SGT = s.SGT


def dt(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=SGT)


def cfg(tmp_path=None, **kw):
    base = dict(report_date=date(2026, 9, 14), output_dir=Path(tmp_path or "out"))
    base.update(kw)
    return s.Config(**base)


def flight(sched, num="TR416", dest="BKI", city="Kota Kinabalu", country="MY", **kw):
    return s.Flight(sched_dt=sched, flight_number=num, codeshares=kw.pop("codeshares", []),
                    airline=kw.pop("airline", "Scoot"), dest_city=city, dest_code=dest,
                    country=country, **kw)


# --------------------------------------------------------------------------- dates & window
class TestWindow:
    def test_ten_intervals_with_labels(self):
        ivs = s.build_intervals(date(2026, 9, 14))
        assert [iv.label for iv in ivs] == [
            "06:00–08:00", "08:00–10:00", "10:00–12:00", "12:00–14:00", "14:00–16:00",
            "16:00–18:00", "18:00–20:00", "20:00–22:00", "22:00–00:00", "00:00–02:00"]
        assert ivs[-1].start == dt(2026, 9, 15, 0, 0) and ivs[-1].end == dt(2026, 9, 15, 2, 0)
        assert ivs[-2].end == dt(2026, 9, 15, 0, 0)

    @pytest.mark.parametrize("when,label", [
        (dt(2026, 9, 14, 6, 0), "06:00–08:00"),      # first minute
        (dt(2026, 9, 14, 23, 59), "22:00–00:00"),    # last minute of selected date
        (dt(2026, 9, 15, 0, 0), "00:00–02:00"),      # next day 00:00
        (dt(2026, 9, 15, 1, 59), "00:00–02:00"),     # next day 01:59
        (dt(2026, 9, 15, 2, 0), None),               # 02:00 excluded
        (dt(2026, 9, 14, 5, 59), None),
        (dt(2026, 9, 14, 0, 30), None),              # 00:30 on the SELECTED date is not the last bucket
        (dt(2026, 9, 14, 1, 59), None),
    ])
    def test_boundaries(self, when, label):
        iv = s.interval_for(when, s.build_intervals(date(2026, 9, 14)))
        assert (iv.label if iv else None) == label

    @pytest.mark.parametrize("d,nxt", [(date(2026, 9, 30), date(2026, 10, 1)),
                                       (date(2026, 12, 31), date(2027, 1, 1)),
                                       (date(2028, 2, 28), date(2028, 2, 29))])
    def test_month_and_year_end(self, d, nxt):
        last = s.build_intervals(d)[-1]
        assert last.start.date() == nxt and last.start.hour == 0

    def test_timezone_aware(self):
        start, end = s.reporting_window(date(2026, 9, 14))
        assert start.utcoffset().total_seconds() == 8 * 3600
        assert (end - start).total_seconds() == 20 * 3600


class TestDateText:
    @pytest.mark.parametrize("text,expected", [
        ("Sat, 10 Oct 2026", date(2026, 10, 10)),
        ("10 October 2026", date(2026, 10, 10)),
        ("Oct 10, 2026", date(2026, 10, 10)),
        ("10/10/2026", date(2026, 10, 10)),
        ("Today, 10 Oct", date(2026, 10, 10)),
    ])
    def test_formats(self, text, expected):
        assert s.parse_date_text(text, date(2026, 10, 9)) == expected

    def test_year_inferred_across_new_year(self):
        assert s.parse_date_text("Fri, 1 Jan", date(2026, 12, 31)) == date(2027, 1, 1)

    def test_month_header_is_not_a_date(self):
        assert s.parse_date_text("OCT 2026", date(2026, 10, 1), full_line=True) is None


# --------------------------------------------------------------------------- line parsing
class TestLineParsing:
    @pytest.mark.parametrize("line,nums,airline", [
        ("TR416Scoot", ["TR416"], "Scoot"),
        ("TR416 · Scoot", ["TR416"], "Scoot"),
        ("TR 416 Scoot", ["TR416"], "Scoot"),
        ("3K685Jetstar Asia", ["3K685"], "Jetstar Asia"),
        ("SQ 106, LH 9771 Singapore Airlines", ["SQ106", "LH9771"], "Singapore Airlines"),
        ("MH5980", ["MH5980"], ""),
    ])
    def test_flight_lines(self, line, nums, airline):
        assert s.parse_flight_line(line) == (nums, airline)

    @pytest.mark.parametrize("line", ["C22", "D41", "A380", "T1", "Kuching (KCH)", "SHOW LATER FLIGHTS", "Gate Open"])
    def test_not_flight_lines(self, line):
        assert s.parse_flight_line(line) is None

    def test_time_lines(self):
        assert s.parse_time_line("17:15")["time"] == dtime(17, 15)
        est = s.parse_time_line("Est 18:30")
        assert est["explicit_est"] and est["time"] == dtime(18, 30)
        dep = s.parse_time_line("Departed 17:22")
        assert dep["status"] == "Departed" and dep["explicit_est"]
        assert s.parse_time_line("00:40 (+1)")["plus_one"]
        assert s.parse_time_line("TR416 17:15") is None

    def test_combined_time_and_flight_split(self):
        assert s.split_combined_line("17:15TR416Scoot") == ["17:15", "TR416Scoot"]


ROW_TEXT = f"""Sat, 14 Sep 2026
{s.ROW_START}
17:15
TR416Scoot
Kota Kinabalu (BKI)
Gate Open
{s.ROW_END}
{s.ROW_START}
18:00
SQ 106 · Singapore Airlines
NH 6254 · ANA
Tokyo (NRT)
Delayed
18:45
{s.ROW_END}
{s.ROW_START}
19:05
3K 685
Jetstar Asia
Bangkok (BKK)
Cancelled
{s.ROW_END}
{s.ROW_START}
20:10
TR 288 · Scoot
Retimed 21:00
Phuket (HKT)
T1
{s.ROW_END}
SHOW LATER FLIGHTS
"""


class TestListingParser:
    def test_row_blocks(self):
        res = s.parse_listing_text(ROW_TEXT, date(2026, 9, 14))
        assert res.row_blocks == 4 and not res.unparsed_rows
        f = {r.flight_numbers[0]: r for r in res.flights}
        assert f["TR416"].dest_code == "BKI" and f["TR416"].airline == "Scoot" and f["TR416"].status == "Gate Open"
        assert f["SQ106"].flight_numbers == ["SQ106", "NH6254"]       # codeshare in one row
        assert f["SQ106"].status == "Delayed" and f["SQ106"].est_time == dtime(18, 45)
        assert f["SQ106"].sched_time == dtime(18, 0)
        assert f["3K685"].status == "Cancelled" and f["3K685"].airline == "Jetstar Asia"
        assert f["TR288"].status == "Retimed" and f["TR288"].est_time == dtime(21, 0)
        assert f["TR288"].terminal == "1"
        assert all(r.explicit_date == date(2026, 9, 14) for r in res.flights)

    def test_original_free_text_format(self):
        text = "Departures\n17:15\nTR434 · Scoot\nKuching (KCH)\nOn Time\n17:20\nSQ 978 · Singapore Airlines\nBangkok (BKK)\n"
        res = s.parse_listing_text(text, date(2026, 9, 14))
        assert [(r.flight_numbers[0], r.sched_time, r.dest_code) for r in res.flights] == [
            ("TR434", dtime(17, 15), "KCH"), ("SQ978", dtime(17, 20), "BKK")]
        assert res.flights[0].status == "On Time"

    def test_flights_sharing_a_time_header(self):
        text = "09:00\nTR100 Scoot\nPenang (PEN)\nSQ200 Singapore Airlines\nSydney (SYD)\n"
        res = s.parse_listing_text(text, date(2026, 9, 14))
        assert [r.sched_time for r in res.flights] == [dtime(9, 0), dtime(9, 0)]

    def test_missing_destination_reported(self):
        text = f"{s.ROW_START}\n10:00\nTR100 Scoot\nDestination TBC\n{s.ROW_END}"
        res = s.parse_listing_text(text, date(2026, 9, 14))
        assert not res.flights and len(res.unparsed_rows) == 1

    def test_date_separators(self):
        text = ("Mon, 14 Sep\n23:50\nTR1 Scoot\nPerth (PER)\n"
                "Tue, 15 Sep\n00:20\nTR2 Scoot\nPenang (PEN)\n")
        res = s.parse_listing_text(text, date(2026, 9, 14))
        dated = s.assign_dates(res.flights, date(2026, 9, 14))
        assert [d for _, d, _ in dated] == [date(2026, 9, 14), date(2026, 9, 15)]

    def test_inferred_midnight_rollover(self):
        text = "".join(f"{t}\nTR{i} Scoot\nPenang (PEN)\n" for i, t in
                       enumerate(["21:00", "22:00", "23:00", "23:55", "00:10", "01:00"]))
        res = s.parse_listing_text(text, date(2026, 9, 14))
        dates = [d for _, d, _ in s.assign_dates(res.flights, date(2026, 9, 14))]
        assert dates == [date(2026, 9, 14)] * 4 + [date(2026, 9, 15)] * 2

    def test_rollover_anchor_handles_previous_day_rows(self):
        times = ["23:30", "23:50"] + [f"{h:02d}:00" for h in range(0, 10)]
        text = "".join(f"{t}\nTR{i} Scoot\nPenang (PEN)\n" for i, t in enumerate(times))
        res = s.parse_listing_text(text, date(2026, 9, 15))
        dates = [d for _, d, _ in s.assign_dates(res.flights, date(2026, 9, 15))]
        assert dates[:2] == [date(2026, 9, 14)] * 2 and set(dates[2:]) == {date(2026, 9, 15)}

    def test_annotate_text(self):
        body = "Header\n10:00\nTR1 Scoot\nPenang (PEN)\nFooter"
        out, located = s.annotate_text(body, ["10:00\nTR1 Scoot\nPenang (PEN)"])
        assert located == 1 and out.splitlines() == ["Header", s.ROW_START, "10:00", "TR1 Scoot",
                                                     "Penang (PEN)", s.ROW_END, "Footer"]


# --------------------------------------------------------------------------- countries
class TestCountry:
    @pytest.fixture
    def resolver(self):
        return s.CountryResolver()

    @pytest.mark.parametrize("city,code,cc", [
        ("Kuala Lumpur", "KUL", "MY"), ("Bangkok", "BKK", "TH"), ("Jakarta", "CGK", "ID"),
        ("Manila", "MNL", "PH"), ("Ho Chi Minh City", "SGN", "VN"), ("Hong Kong", "HKG", "HK"),
        ("Taipei", "TPE", "TW"), ("Shanghai", "PVG", "CN"), ("Tokyo", "HND", "JP"),
        ("Seoul", "ICN", "KR"), ("Sydney", "SYD", "AU"), ("Auckland", "AKL", "NZ"),
        ("London", "LHR", "GB"), ("Dubai", "DXB", "AE"), ("Doha", "DOH", "QA"),
        ("Los Angeles", "LAX", "US"), ("Macau", "MFM", "MO"),
    ])
    def test_examples(self, resolver, city, code, cc):
        assert resolver.resolve(city, code)[0] == cc

    def test_city_fallback_and_spellings(self):
        r = s.CountryResolver(use_dataset=False)
        assert r.resolve("Bengaluru", "")[0] == "IN"
        assert r.resolve("  ho chi minh  ", "")[0] == "VN"
        assert r.resolve("Malé", "")[0] == "MV"
        assert r.resolve("Bangkok Suvarnabhumi International Airport", "")[0] == "TH"
        assert r.resolve("Xi'an", "XXX")[0] == "CN"

    def test_iata_table_without_dataset(self):
        r = s.CountryResolver(use_dataset=False)
        assert r.resolve("Somewhere", "BKI") == ("MY", "iata-table")

    def test_unknown_reported_not_dropped(self):
        r = s.CountryResolver(use_dataset=False)
        assert r.resolve("Atlantis", "QQQ") == ("??", "unknown")
        assert r.unknown["Atlantis (QQQ)"] == 1

    def test_iata_code_is_never_used_as_country(self, resolver):
        cc, _ = resolver.resolve("Kota Kinabalu", "BKI")
        assert len(cc) == 2 and cc == "MY"


# --------------------------------------------------------------------------- dedupe
class TestDedupe:
    def test_exact_duplicates(self):
        a, b = flight(dt(2026, 9, 14, 17, 15)), flight(dt(2026, 9, 14, 17, 15))
        out, log = s.dedupe_flights([a, b])
        assert len(out) == 1 and log

    def test_same_number_different_day_kept(self):
        out, _ = s.dedupe_flights([flight(dt(2026, 9, 14, 17, 15)), flight(dt(2026, 9, 15, 17, 15))])
        assert len(out) == 2

    def test_codeshare_rows_merged_auto(self):
        a = flight(dt(2026, 9, 14, 18, 0), "SQ106", "NRT", "Tokyo", "JP", order=1)
        b = flight(dt(2026, 9, 14, 18, 0), "NH6254", "NRT", "Tokyo", "JP", order=2)
        out, _ = s.dedupe_flights([a, b], "auto")
        assert len(out) == 1 and out[0].flight_number == "SQ106" and out[0].codeshares == ["NH6254"]

    def test_codeshare_rows_not_merged_strict(self):
        a = flight(dt(2026, 9, 14, 18, 0), "SQ106", "NRT")
        b = flight(dt(2026, 9, 14, 18, 0), "NH6254", "NRT")
        assert len(s.dedupe_flights([a, b], "strict")[0]) == 2

    def test_conflicting_gates_or_same_carrier_not_merged(self):
        a = flight(dt(2026, 9, 14, 18, 0), "SQ106", "KUL", gate="C1")
        b = flight(dt(2026, 9, 14, 18, 0), "MH602", "KUL", gate="D40")
        c = flight(dt(2026, 9, 14, 18, 0), "SQ118", "KUL")
        assert len(s.dedupe_flights([a, b], "auto")[0]) == 2
        assert len(s.dedupe_flights([flight(dt(2026, 9, 14, 18, 0), "SQ106", "KUL"), c], "auto")[0]) == 2

    def test_cancelled_not_merged_with_operating(self):
        a = flight(dt(2026, 9, 14, 18, 0), "SQ106", "NRT", status="Cancelled")
        b = flight(dt(2026, 9, 14, 18, 0), "NH6254", "NRT")
        assert len(s.dedupe_flights([a, b])[0]) == 2

    def test_row_codeshare_and_separate_row_share_number(self):
        a = flight(dt(2026, 9, 14, 18, 0), "SQ106", "NRT", codeshares=["NH6254"])
        b = flight(dt(2026, 9, 14, 18, 0), "NH6254", "NRT")
        assert len(s.dedupe_flights([a, b], "strict")[0]) == 1


# --------------------------------------------------------------------------- policies & aggregation
class TestPoliciesAndAggregation:
    def setup_method(self):
        self.ivs = s.build_intervals(date(2026, 9, 14))

    def test_cancelled_policy(self, tmp_path):
        f = flight(dt(2026, 9, 14, 7, 0), status="Cancelled")
        s.apply_policies([f], cfg(tmp_path), self.ivs)
        assert not f.counted and f.exclusion_reason == "cancelled"
        s.apply_policies([f], cfg(tmp_path, cancelled_policy="include"), self.ivs)
        assert f.counted

    def test_retimed_time_basis(self, tmp_path):
        f = flight(dt(2026, 9, 14, 7, 50), status="Retimed", est_dt=dt(2026, 9, 14, 8, 30))
        s.apply_policies([f], cfg(tmp_path), self.ivs)
        assert f.interval_label == "06:00–08:00"
        s.apply_policies([f], cfg(tmp_path, time_basis="estimated"), self.ivs)
        assert f.interval_label == "08:00–10:00"

    def test_retimed_past_window_end(self, tmp_path):
        f = flight(dt(2026, 9, 15, 1, 50), est_dt=dt(2026, 9, 15, 2, 20))
        s.apply_policies([f], cfg(tmp_path, time_basis="estimated"), self.ivs)
        assert not f.counted

    def test_other_terminal_and_sin_excluded(self, tmp_path):
        a = flight(dt(2026, 9, 14, 7, 0), terminal="3")
        b = flight(dt(2026, 9, 14, 7, 0), "SQ1", "SIN", "Singapore")
        s.apply_policies([a, b], cfg(tmp_path), self.ivs)
        assert not a.counted and not b.counted

    def test_totals_match(self, tmp_path):
        fl = [flight(dt(2026, 9, 14, 6, 0), "A1", country="MY"),
              flight(dt(2026, 9, 14, 6, 30), "A2", country="TH"),
              flight(dt(2026, 9, 14, 23, 59), "A3", country="MY"),
              flight(dt(2026, 9, 15, 0, 0), "A4", country="AU"),
              flight(dt(2026, 9, 15, 1, 59), "A5", country="??"),
              flight(dt(2026, 9, 15, 2, 0), "A6", country="MY"),
              flight(dt(2026, 9, 14, 1, 0), "A7", country="MY")]
        s.apply_policies(fl, cfg(tmp_path), self.ivs)
        b = s.aggregate(fl, self.ivs)
        assert b["06:00–08:00"] == Counter({"MY": 1, "TH": 1})
        assert b["22:00–00:00"] == Counter({"MY": 1})
        assert b["00:00–02:00"] == Counter({"AU": 1, "??": 1})
        assert sum(sum(c.values()) for c in b.values()) == 5 == sum(f.counted for f in fl)


# --------------------------------------------------------------------------- end-to-end (offline)
def test_finalise_writes_csvs(tmp_path):
    main_text = (f"Mon, 14 Sep 2026\n{s.ROW_START}\n05:55\nTR9 Scoot\nPenang (PEN)\n{s.ROW_END}\n"
                 f"{s.ROW_START}\n06:00\nTR416Scoot\nKota Kinabalu (BKI)\n{s.ROW_END}\n"
                 f"{s.ROW_START}\n06:00\nSQ 106 · Singapore Airlines\nTokyo (NRT)\n{s.ROW_END}\n"
                 f"{s.ROW_START}\n06:00\nNH 6254 · ANA\nTokyo (NRT)\n{s.ROW_END}\n"
                 f"{s.ROW_START}\n12:00\n3K 685 Jetstar Asia\nBangkok (BKK)\nCancelled\n{s.ROW_END}\n"
                 f"{s.ROW_START}\n23:59\nTR8 Scoot\nAtlantis (QQQ)\n{s.ROW_END}\n")
    next_text = (f"Tue, 15 Sep 2026\n{s.ROW_START}\n00:00\nTR1 Scoot\nPerth (PER)\n{s.ROW_END}\n"
                 f"{s.ROW_START}\n01:59\nTR2 Scoot\nSydney (SYD)\n{s.ROW_END}\n"
                 f"{s.ROW_START}\n02:00\nTR3 Scoot\nMelbourne (MEL)\n{s.ROW_END}\n")
    c = cfg(tmp_path)
    pages = [(date(2026, 9, 14), s.parse_listing_text(main_text, date(2026, 9, 14)).flights),
             (date(2026, 9, 15), s.parse_listing_text(next_text, date(2026, 9, 15)).flights)]
    res = s.finalise(c, pages, [], complete=True)
    assert res.detail_csv.name == "changi_t1_departures_2026-09-14.csv"
    assert res.summary_csv.name == "changi_t1_country_summary_2026-09-14.csv"
    with res.summary_csv.open(encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["Time Interval", "Country Code", "Flights"]
    assert ["06:00–08:00", "JP", "1"] in rows and ["06:00–08:00", "MY", "1"] in rows
    assert ["00:00–02:00", "AU", "2"] in rows and ["22:00–00:00", "??", "1"] in rows
    with res.detail_csv.open(encoding="utf-8-sig") as fh:
        det = list(csv.DictReader(fh))
    nums = {r["flight_number"]: r for r in det}
    assert "TR9" not in nums and "TR3" not in nums                 # outside window
    assert nums["SQ106"]["codeshare_flight_numbers"] == "NH6254"   # codeshare merged
    assert nums["3K685"]["counted"] == "no" and nums["3K685"]["exclusion_reason"] == "cancelled"
    assert nums["TR1"]["scheduled_date"] == "2026-09-15"
    total = sum(int(r[2]) for r in rows[1:])
    assert total == sum(1 for r in det if r["counted"] == "yes") == 5
    assert "TOTAL" in res.report and "Atlantis (QQQ)" in res.report


def test_wrong_date_listing_is_rejected(tmp_path):
    text = f"Sun, 13 Sep 2026\n{s.ROW_START}\n10:00\nTR1 Scoot\nPenang (PEN)\n{s.ROW_END}"
    pages = [(date(2026, 9, 14), s.parse_listing_text(text, date(2026, 9, 14)).flights)]
    with pytest.raises(s.IncompleteDataError):
        s.finalise(cfg(tmp_path), pages, [], complete=True)


# --------------------------------------------------------------------------- live-site regressions (Oct 2026 capture)
LIVE_ROWS = [  # exact innerText of rows captured from changiairport.com, 21 Oct 2026
    "00:30\nZG54ZIPAIR\nTokyo (Narita) (NRT)\nT1\nCheck-In Row 14\nGATE INFO AT 20:30",
    "00:35\nTR828Scoot\nJeju (CJU)\nCodeshare\nSQ8490\nT1\nCheck-In Row 7\nGATE INFO AT 20:35",
    "00:50\nEK353Emirates\nDubai (DXB)\nCodeshare\nQF8353TP6356\nT1\nCheck-In Row 13\nGATE INFO AT 20:50",
    "00:55\nKL836KLM Royal Dutch Airlines\nAmsterdam (AMS)\nCodeshare\nAF8218QF4235\nT1\nCheck-In Row 2\nGATE INFO AT 20:55",
    "01:00\nTR866Scoot\nTokyo (Narita) (NRT)\nvia Taipei\nCodeshare\nSQ8646\nT1\nCheck-In Row 7\nGATE INFO AT 21:00",
    "01:55\nJL38Japan Airlines\nTokyo (Haneda) (HND)\nCodeshare\n6E3814AA8488AS7348QF4017UL3338\nT1\nCheck-In Row 1\nGATE INFO AT 21:55",
]
LIVE_HREF = "https://www.changiairport.com/en/fly/flight-information/departures/flight-detail.html?flight_id={}_20261021"


class TestLiveFormats:
    def parse(self, with_dates=True):
        dates = [date(2026, 10, 21)] * len(LIVE_ROWS) if with_dates else None
        body = "Pick a date\nSHOW EARLIER FLIGHTS\n" + "\n".join(LIVE_ROWS) + "\nSHOW LATER FLIGHTS\nCareers\nCAI\nJewel"
        text, located = s.annotate_text(body, LIVE_ROWS, dates)
        assert located == len(LIVE_ROWS)
        return s.parse_listing_text(text, date(2026, 10, 21))

    def test_rows(self):
        res = self.parse()
        assert not res.unparsed_rows and len(res.flights) == 6
        f = {r.flight_numbers[0]: r for r in res.flights}
        assert (f["ZG54"].airline, f["ZG54"].dest_city, f["ZG54"].dest_code) == ("ZIPAIR", "Tokyo (Narita)", "NRT")
        assert f["ZG54"].est_time is None                     # "GATE INFO AT 20:30" is not a departure time
        assert f["ZG54"].checkin == "14" and f["ZG54"].terminal == "1"
        assert f["EK353"].flight_numbers == ["EK353", "QF8353", "TP6356"]
        assert f["KL836"].airline == "KLM Royal Dutch Airlines"
        assert f["TR866"].via == ["Taipei"]
        assert f["JL38"].flight_numbers == ["JL38", "6E3814", "AA8488", "AS7348", "QF4017", "UL3338"]
        assert f["JL38"].dest_code == "HND"                   # not the footer's "Careers / CAI"
        assert all(r.date_from_link and r.explicit_date == date(2026, 10, 21) for r in res.flights)

    def test_free_text_fallback(self):
        text = "SHOW EARLIER FLIGHTS\n" + "\n".join(LIVE_ROWS) + "\nSHOW LATER FLIGHTS\nCareers\nCAI\n"
        res = s.parse_listing_text(text, date(2026, 10, 21))
        assert len(res.flights) == 6 and not res.unparsed_rows
        assert {r.flight_numbers[0]: len(r.flight_numbers) for r in res.flights}["JL38"] == 6

    def test_codeshare_on_one_line(self):
        res = s.parse_listing_text(f"{s.ROW_START}\n00:35\nTR828Scoot\nJeju (CJU)\nCodeshare SQ8490\n{s.ROW_END}", date(2026, 10, 21))
        assert res.flights[0].flight_numbers == ["TR828", "SQ8490"]

    def test_link_dates_override_inference(self):
        res = self.parse()
        # Rows of the 21 Oct page start at 00:30: without link dates they'd still be the 21st,
        # but a 20 Oct page listing these rows must date them 21 Oct from the links.
        dated = s.assign_dates(res.flights, date(2026, 10, 20))
        assert {d for _, d, _ in dated} == {date(2026, 10, 21)}
        assert {src for _, _, src in dated} == {"row-link"}

    def test_row_link_date(self):
        assert s.row_link_date(LIVE_HREF.format("ZG54"), date(2026, 10, 21)) == date(2026, 10, 21)
        assert s.row_link_date("flight-detail.html?flight_id=SQ1", date(2026, 10, 21)) is None
        assert s.row_link_date("x?id=20991231", date(2026, 10, 21)) is None   # implausible date ignored

    @pytest.mark.parametrize("ev,ok", [
        ({"shown": date(2026, 10, 21), "url": None, "rows": Counter({date(2026, 10, 21): 10})}, True),
        ({"shown": date(2026, 10, 20), "url": date(2026, 10, 21), "rows": Counter()}, False),
        ({"shown": None, "url": date(2026, 10, 21), "rows": Counter()}, True),
        ({"shown": date(2026, 10, 21), "url": None, "rows": Counter({date(2026, 10, 20): 9, date(2026, 10, 21): 1})}, False),
        ({"shown": None, "url": None, "rows": Counter()}, False),
    ])
    def test_date_confirmed(self, ev, ok):
        assert s.date_confirmed(ev, date(2026, 10, 21))[0] is ok


# --------------------------------------------------------------------------- Word / PDF report
class TestReportDocuments:
    def make(self, tmp_path, fmt="both", status="COMPLETE"):
        c = cfg(tmp_path, doc_format=fmt)
        ivs = s.build_intervals(c.report_date)
        b = {iv.label: Counter() for iv in ivs}
        b["06:00–08:00"].update({"MY": 3, "CN": 2, "HK": 1})
        b["00:00–02:00"].update({"CN": 1, "AU": 2})
        rt = s.build_report_table(b, ivs, c, status, [], Counter())
        return c, rt

    def test_china_and_totals(self, tmp_path):
        _, rt = self.make(tmp_path)
        first, last = rt.rows[0], rt.rows[-1]
        assert (first["flights"], first["china"]) == (6, 2)      # HK is not counted as China
        assert first["countries"] == "MY 3   CN 2   HK 1"
        assert (last["day"], last["flights"], last["china"]) == ("Tue 15 Sep", 3, 1)
        assert rt.rows[1]["countries"] == "–" and rt.rows[1]["flights"] == 0
        assert (rt.total_flights, rt.total_china) == (9, 3)
        assert rt.date_line == "Monday, 14 September 2026"

    def test_files_written(self, tmp_path):
        pytest.importorskip("docx")
        pytest.importorskip("reportlab")
        c, rt = self.make(tmp_path)
        paths = s.write_report_documents(c, rt, "")
        assert [p.name for p in paths] == ["changi_t1_report_2026-09-14.docx", "changi_t1_report_2026-09-14.pdf"]
        from docx import Document
        doc = Document(str(paths[0]))
        tbl = doc.tables[0]
        assert len(tbl.rows) == 12                                   # header + 10 intervals + total
        assert [c.text for c in tbl.rows[-1].cells[:3]] == ["TOTAL", "9", "3"]
        assert "Monday, 14 September 2026" in "\n".join(p.text for p in doc.paragraphs)
        assert paths[1].read_bytes()[:4] == b"%PDF"

    def test_none_and_partial(self, tmp_path):
        c, rt = self.make(tmp_path, fmt="none")
        assert s.write_report_documents(c, rt, "") == []
        _, rt2 = self.make(tmp_path, status="PARTIAL — see notes")
        assert not rt2.status.startswith("COMPLETE")


# --------------------------------------------------------------------------- terminals
class TestTerminals:
    def test_url_and_file_prefix(self, tmp_path):
        c = cfg(tmp_path, terminal="3")
        assert c.site_url.endswith("departures.html?terminal=3")
        assert c.file_prefix == "changi_t3"
        assert cfg(tmp_path, url="file:///x.html?terminal=1").site_url == "file:///x.html?terminal=1"

    def test_report_title_and_names(self, tmp_path):
        c = cfg(tmp_path, terminal="3", doc_format="none")
        text = f"Mon, 14 Sep 2026\n{s.ROW_START}\n07:00\nTR1 Scoot\nPenang (PEN)\nT3\n{s.ROW_END}"
        nxt = f"Tue, 15 Sep 2026\n{s.ROW_START}\n00:10\nTR2 Scoot\nPerth (PER)\nT3\n{s.ROW_END}"
        pages = [(date(2026, 9, 14), s.parse_listing_text(text, date(2026, 9, 14)).flights),
                 (date(2026, 9, 15), s.parse_listing_text(nxt, date(2026, 9, 15)).flights)]
        res = s.finalise(c, pages, [], complete=True)
        assert res.detail_csv.name == "changi_t3_departures_2026-09-14.csv"
        assert "Terminal 3" in res.report and sum(sum(v.values()) for v in res.buckets.values()) == 2
        import json
        rep = json.loads((tmp_path / "reports" / "changi_t3_2026-09-14.json").read_text(encoding="utf-8"))
        assert rep["terminal"] == "3" and rep["total_flights"] == 2 and "Terminal 3" in rep["title"]
        assert rep["files"]["csv"] == "changi_t3_departures_2026-09-14.csv"

    def test_wrong_terminal_detected(self):
        raws = s.parse_listing_text(
            "".join(f"{s.ROW_START}\n0{i}:00\nTR{i} Scoot\nPenang (PEN)\nT1\n{s.ROW_END}\n" for i in range(6, 9)),
            date(2026, 9, 14)).flights
        s.check_terminal(raws, "1")
        with pytest.raises(s.ListingLoadError):
            s.check_terminal(raws, "3")
        s.check_terminal([], "3")   # nothing to check → no error (warning only)
