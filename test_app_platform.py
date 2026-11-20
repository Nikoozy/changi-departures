"""App behaviour that differs per platform (Mac / Windows) — runs offline, no browser.
macOS is simulated by patching the platform flags; the real .app build must be
checked on a Mac (see README)."""
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app  # noqa: E402


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


def test_mac_app_saves_reports_in_documents(fake_home, monkeypatch, tmp_path):
    monkeypatch.setattr(app, "IS_MAC", True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    bundle = tmp_path / "Changi Departures.app" / "Contents" / "MacOS"
    bundle.mkdir(parents=True)
    monkeypatch.setattr(sys, "executable", str(bundle / "ChangiDepartures"))
    out = app.pick_output_dir(None)
    assert out == fake_home / "Documents" / "ChangiReports"
    assert not (bundle / "ChangiReports").exists()          # never writes inside the bundle
    assert app.settings_candidates() == [fake_home / "Documents" / "ChangiReports" / "settings.json"]


def test_windows_keeps_reports_next_to_app(fake_home, monkeypatch, tmp_path):
    monkeypatch.setattr(app, "IS_MAC", False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    folder = tmp_path / "ChangiDepartures"
    folder.mkdir()
    monkeypatch.setattr(sys, "executable", str(folder / "ChangiDepartures.exe"))
    assert app.pick_output_dir(None) == folder / "ChangiReports"
    assert app.settings_candidates()[0] == folder / "settings.json"


def test_explicit_output_dir_expands_tilde(fake_home):
    assert app.pick_output_dir("~/Reports") == fake_home / "Reports"


def test_open_folder_command_per_platform(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(app.subprocess, "Popen", lambda args, **kw: calls.append(args))
    monkeypatch.setattr(app, "IS_WIN", False)
    monkeypatch.setattr(app, "IS_MAC", True)
    app.open_in_file_manager(tmp_path)
    monkeypatch.setattr(app, "IS_MAC", False)
    app.open_in_file_manager(tmp_path)
    assert calls == [["open", str(tmp_path)], ["xdg-open", str(tmp_path)]]


def test_finder_psn_argument_is_ignored(monkeypatch):
    seen = {}

    def stop(*a, **k):
        raise SystemExit(0)

    monkeypatch.setattr(app, "pick_output_dir", lambda explicit: seen.setdefault("ok", True) and stop())
    with pytest.raises(SystemExit):
        app.main(["-psn_0_12345", "--no-browser"])
    assert seen["ok"]                                       # argparse didn't reject -psn_...


def _server(tmp_path, port):
    state = app.AppState(tmp_path, "1", None, "auto", None, None)
    srv = app.make_server(state, port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


def _post(port, path, body=b"{}"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Requested-With": "changi-app"})
    return urllib.request.urlopen(req)


def test_quit_endpoint(tmp_path, monkeypatch):
    quits = []
    monkeypatch.setattr(app, "hard_exit", lambda delay=0.6: quits.append(delay))
    srv, state = _server(tmp_path, 8795)
    try:
        # refuses while a report is running, unless forced
        job = app.Job("1", app.s.today_in_singapore(), False)
        state.current = job
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(8795, "/api/quit")
        assert e.value.code == 409 and not quits
        assert json.loads(_post(8795, "/api/quit", b'{"force": true}').read())["ok"]
        job.state = "done"
        assert json.loads(_post(8795, "/api/quit").read())["ok"]
        assert len(quits) == 2
    finally:
        srv.shutdown()


def test_page_has_quit_button():
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="quitApp"' in html and "/api/quit" in html


# --------------------------------------------------------------------------- auto-quit (Mac)
class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_tracker_quits_after_page_closed():
    clk = FakeClock()
    pt = app.PageTracker(grace=8, stale=150, startup=180, clock=clk)
    tab = pt.hello()
    clk.t += 60
    assert pt.beat(tab) and not pt.should_quit(False)
    assert pt.bye(tab)
    clk.t += 5
    assert not pt.should_quit(False)            # within grace (e.g. a page refresh)
    clk.t += 4
    assert pt.should_quit(False)


def test_tracker_refresh_does_not_quit():
    clk = FakeClock()
    pt = app.PageTracker(clock=clk)
    old = pt.hello()
    pt.bye(old)
    clk.t += 1
    pt.hello()                                   # reloaded page says hello again
    clk.t += 60
    assert not pt.should_quit(False)


def test_tracker_waits_for_running_report():
    clk = FakeClock()
    pt = app.PageTracker(clock=clk)
    pt.bye(pt.hello())
    clk.t += 30
    assert not pt.should_quit(True)              # report still running
    assert pt.should_quit(False)


def test_tracker_background_tab_and_crash():
    clk = FakeClock()
    pt = app.PageTracker(clock=clk)
    tab = pt.hello()
    clk.t += 70                                  # background tab: heartbeat slowed to ~1/min
    assert not pt.should_quit(False)
    pt.beat(tab)
    clk.t += 151                                 # browser crashed: no bye, no heartbeats
    assert not pt.should_quit(False)             # just went stale - grace starts now
    clk.t += 9
    assert pt.should_quit(False)


def test_tracker_startup_window_and_unknown_tabs():
    clk = FakeClock()
    pt = app.PageTracker(clock=clk)
    clk.t += 170
    assert not pt.should_quit(False)             # browser may still be opening
    clk.t += 20
    assert pt.should_quit(False)                 # page never opened
    assert not pt.bye("not-a-tab") and not pt.beat("not-a-tab")


def test_bye_endpoint_needs_valid_tab_but_no_header(tmp_path, monkeypatch):
    srv, state = _server(tmp_path, 8796)
    try:
        tab = json.loads(_post(8796, "/api/hello").read())["tab"]
        assert json.loads(_post(8796, "/api/heartbeat", json.dumps({"tab": tab}).encode()).read())["ok"]

        def beacon(body):  # what navigator.sendBeacon sends: text/plain, no custom header
            req = urllib.request.Request("http://127.0.0.1:8796/api/bye", data=body, method="POST",
                                         headers={"Content-Type": "text/plain;charset=UTF-8"})
            return urllib.request.urlopen(req)

        with pytest.raises(urllib.error.HTTPError) as e:
            beacon(b'{"tab": "guess"}')
        assert e.value.code == 404 and state.pages.pages      # a wrong id changes nothing
        assert beacon(json.dumps({"tab": tab}).encode()).status == 200
        assert not state.pages.pages
        with pytest.raises(urllib.error.HTTPError) as e:   # hello still needs the app header
            urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8796/api/hello", data=b"{}", method="POST"))
        assert e.value.code == 403
    finally:
        srv.shutdown()


def test_auto_quit_loop_calls_exit(tmp_path):
    state = app.AppState(tmp_path, "1", None, "auto", None, None)
    state.pages = app.PageTracker(grace=0.2, stale=150, startup=180)
    state.pages.bye(state.pages.hello())
    quit_called = threading.Event()
    state.start_auto_quit(exit_fn=quit_called.set, every=0.1)
    assert quit_called.wait(3)
