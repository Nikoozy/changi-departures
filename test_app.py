"""Desktop-app tests: the local web server + page, driven by a real browser against
the mock Changi page. No internet needed.  Run:  python -m pytest -m browser tests/test_app.py
"""
import json
import os
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402
import changi_scraper as s  # noqa: E402

pytestmark = pytest.mark.browser
MOCK = (Path(__file__).parent / "mock_site" / "departures.html").resolve().as_uri()
SCREENSHOT_DIR = os.environ.get("APP_SCREENSHOT_DIR")


def start_app(tmp_path, url_template, port):
    state = app.AppState(tmp_path, "1", url_template, "auto",
                         os.environ.get("CHROME_BINARY"), os.environ.get("CHROMEDRIVER"))
    srv = app.make_server(state, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


def ui_driver():
    cfg = s.Config(report_date=s.today_in_singapore(), chrome_binary=os.environ.get("CHROME_BINARY"),
                   chromedriver=os.environ.get("CHROMEDRIVER"))
    d = s.build_driver(cfg)
    d.set_window_size(1280, 1800)
    return d


def wait_js(d, expr, timeout):
    from selenium.webdriver.support.ui import WebDriverWait
    WebDriverWait(d, timeout, poll_frequency=0.5).until(lambda drv: drv.execute_script("return " + expr))


def test_generate_terminal_3_report(tmp_path):
    srv, state = start_app(tmp_path, MOCK + "?terminal={terminal}", 8791)
    d = ui_driver()
    try:
        d.get("http://127.0.0.1:8791/?terminal=3")
        wait_js(d, "document.querySelector('#t3') && document.querySelector('#t3').checked", 10)
        assert "No saved report for Terminal 3" in d.find_element("id", "emptyState").text
        d.find_element("id", "generate").click()
        wait_js(d, "!document.getElementById('progress').classList.contains('hidden')", 10)
        if SCREENSHOT_DIR:
            import time; time.sleep(4)
            d.save_screenshot(os.path.join(SCREENSHOT_DIR, "app_running.png"))
        wait_js(d, "!document.getElementById('report').classList.contains('hidden') || "
                   "!document.getElementById('errorCard').classList.contains('hidden')", 300)
        assert d.find_element("id", "errorCard").get_attribute("class").count("hidden"), \
            d.find_element("id", "errorText").text
        assert d.find_element("id", "repTitle").text.startswith("Terminal 3")
        job = next(iter(state.jobs.values()))
        assert any("Selected date verified" in ln for ln in job.log)   # progress messages reach the page
        assert job.percent == 100 and job.step == "Report ready"
        assert d.find_element("id", "kTotal").text == "16"
        rows = d.find_elements("css selector", "#repTable tbody tr")
        assert len(rows) == 11                                   # 10 intervals + total
        assert "TOTAL" in rows[-1].text.upper() and "16" in rows[-1].text
        # history lists it, files are downloadable
        wait_js(d, "document.querySelectorAll('#history tbody tr').length === 1", 10)
        rep = json.loads(urllib.request.urlopen("http://127.0.0.1:8791/api/report?terminal=3&date="
                                                + s.today_in_singapore().isoformat()).read())
        for key in ("pdf", "docx", "csv"):
            assert rep["files"][key].startswith("changi_t3_")
            body = urllib.request.urlopen(f"http://127.0.0.1:8791/files/{rep['files'][key]}").read()
            assert len(body) > 200
        if SCREENSHOT_DIR:
            d.execute_script("window.scrollTo(0,0)")
            d.save_screenshot(os.path.join(SCREENSHOT_DIR, "app_report.png"))
        # reload: saved report shows immediately
        d.get("http://127.0.0.1:8791/?terminal=3")
        wait_js(d, "!document.getElementById('report').classList.contains('hidden')", 10)
        assert "saved report" in d.find_element("id", "cachedNote").text
    finally:
        d.quit()
        srv.shutdown()


def test_wrong_terminal_shows_error(tmp_path):
    # mock ignores the requested terminal and always shows T1 rows
    srv, state = start_app(tmp_path, MOCK + "?terminal={terminal}&showterm=1", 8792)
    d = ui_driver()
    try:
        d.get("http://127.0.0.1:8792/?terminal=3")
        wait_js(d, "document.querySelector('#t3') && document.querySelector('#t3').checked", 10)
        d.find_element("id", "generate").click()
        wait_js(d, "!document.getElementById('errorCard').classList.contains('hidden')", 300)
        assert "showing Terminal 1 flights, not Terminal 3" in d.find_element("id", "errorText").text
        assert not list((tmp_path / "reports").glob("*.json")) if (tmp_path / "reports").exists() else True
    finally:
        d.quit()
        srv.shutdown()


def test_post_requires_app_header(tmp_path):
    srv, _ = start_app(tmp_path, MOCK + "?terminal={terminal}", 8793)
    try:
        req = urllib.request.Request("http://127.0.0.1:8793/api/run", data=b'{"terminal":"1","date":"2026-10-27"}',
                                     method="POST", headers={"Content-Type": "text/plain"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen("http://127.0.0.1:8793/files/..%2Fsecret.txt")
        assert e.value.code == 400
    finally:
        srv.shutdown()


def test_closing_the_page_quits_but_refresh_does_not(tmp_path):
    srv, state = start_app(tmp_path, MOCK + "?terminal={terminal}", 8794)
    state.pages = app.PageTracker(grace=3, stale=150, startup=180)
    quit_called = threading.Event()
    state.start_auto_quit(exit_fn=quit_called.set, every=0.3)
    d = ui_driver()
    try:
        d.get("about:blank")
        first = d.current_window_handle
        d.switch_to.new_window("tab")
        d.get("http://127.0.0.1:8794/?terminal=1")
        wait_js(d, "document.querySelector('#t1') && document.querySelector('#t1').checked", 10)
        import time
        deadline = time.time() + 5
        while not state.pages.pages and time.time() < deadline:
            time.sleep(0.1)
        assert len(state.pages.pages) == 1                      # page said hello
        d.refresh()                                             # refresh: bye + hello within grace
        wait_js(d, "document.querySelector('#t1') && document.querySelector('#t1').checked", 10)
        assert not quit_called.wait(4)
        assert len(state.pages.pages) == 1
        d.close()                                               # close the tab -> beacon -> quit
        d.switch_to.window(first)
        assert quit_called.wait(10)
        assert not state.pages.pages
    finally:
        d.quit()
        srv.shutdown()
