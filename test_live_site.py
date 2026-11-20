"""LIVE test against changiairport.com — needs internet and Chrome.

Skipped unless CHANGI_LIVE=1 is set:
    set CHANGI_LIVE=1            (Windows cmd)
    $env:CHANGI_LIVE="1"         (PowerShell)
    python -m pytest -m live -s
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import changi_scraper as s  # noqa: E402

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(os.environ.get("CHANGI_LIVE") != "1", reason="set CHANGI_LIVE=1 to run")]


def test_live_today(tmp_path):
    cfg = s.Config(report_date=s.today_in_singapore(), output_dir=tmp_path, headless=True)
    res = s.run_live(cfg)
    counted = [f for f in res.flights if f.counted]
    assert counted, "no flights counted"
    assert sum(sum(c.values()) for c in res.buckets.values()) == len(counted)
    print(res.report)
