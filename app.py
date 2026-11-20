#!/usr/bin/env python3
"""
Changi Departures — desktop app for store managers.

Runs a small web server on THIS computer only (http://127.0.0.1:8765) and opens
it in the default browser. Managers pick a terminal and a date, press
"Generate report", and get the departures-by-country report on screen, with
PDF / Word / CSV downloads. Past reports are listed and open instantly.

    python app.py                    # opens the page in the browser
    python app.py --terminal 3       # page defaults to Terminal 3 on this desktop
    python app.py --no-browser       # start the server without opening a browser tab

Windows: keep the black console window open while using the page; closing it stops the app.
macOS:   the app lives in the menu bar (✈ icon: Open / Reports folder / Quit). The page
         also has a "Quit app" button on every platform.
"""
from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import changi_scraper as s

APP_NAME = "Changi Departures"
APP_VERSION = "1.0"
DEFAULT_PORT = 8765
LOG = logging.getLogger("changi.app")


# ----------------------------------------------------------------------------- paths
def app_dir() -> Path:
    """Folder of the .exe when packaged, else the folder of this file."""
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def documents_reports_dir() -> Path:
    return Path.home() / "Documents" / "ChangiReports"


def settings_candidates() -> list[Path]:
    """settings.json next to the app (Windows folder / source checkout), then
    Documents/ChangiReports/settings.json (the place to put it for the Mac app,
    whose bundle must not be edited)."""
    out = []
    if not (IS_MAC and is_frozen()):
        out.append(app_dir() / "settings.json")
    out.append(documents_reports_dir() / "settings.json")
    return out


def open_in_file_manager(path: Path) -> None:
    if IS_WIN:
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def hard_exit(delay: float = 0.6) -> None:
    """Stop the whole app shortly after replying (works from any thread, including
    under the macOS menu-bar loop)."""
    def _bye():
        LOG.info("Quit requested from the page")
        logging.shutdown()
        os._exit(0)
    threading.Timer(delay, _bye).start()


def resource(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


# ----------------------------------------------------------------------------- jobs
PROGRESS_STEPS = [  # (substring in the scraper's log message, percent, friendly text)
    ("Using ", 6, "Browser started"),
    ("Opening ", 10, "Opening the Changi departures page"),
    ("— Scraping", None, None),               # handled specially (first/second day)
    ("Selected date verified", None, "Date selected on the website"),
    ("'Show earlier flights'", None, "Loading earlier flights"),
    ("'Show later flights'", None, "Loading later flights"),
    ("rows parsed", None, "Flights read"),
    ("Codeshare/duplicate merged", 92, "Counting flights by country"),
]


class Job:
    def __init__(self, terminal: str, report_date: date, show_browser: bool):
        self.id = uuid.uuid4().hex[:12]
        self.terminal, self.report_date, self.show_browser = terminal, report_date, show_browser
        self.state = "running"
        self.percent, self.step = 2, "Starting"
        self.log: list[str] = []
        self.started = time.time()
        self.finished: Optional[float] = None
        self.error: Optional[str] = None
        self.report: Optional[dict] = None
        self._day = 0

    def on_log(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if record.levelno >= logging.INFO:
            self.log.append(f"{datetime.now():%H:%M:%S}  {msg}")
            self.log = self.log[-200:]
        for key, pct, text in PROGRESS_STEPS:
            if key not in msg:
                continue
            if key == "— Scraping":
                self._day += 1
                self.percent = 15 if self._day == 1 else 62
                self.step = ("Reading today's flights" if self._day == 1
                             else "Reading next-day flights (00:00–02:00)")
            elif pct is None:
                base = 15 if self._day <= 1 else 62
                span = 45 if self._day <= 1 else 28
                self.percent = min(base + span, max(self.percent + 3, base + 5))
                self.step = text + (" (next day)" if self._day > 1 else "")
            else:
                self.percent, self.step = max(self.percent, pct), text
            break

    def as_dict(self) -> dict:
        return {
            "id": self.id, "terminal": self.terminal, "date": self.report_date.isoformat(),
            "state": self.state, "percent": self.percent, "step": self.step,
            "elapsed": round((self.finished or time.time()) - self.started),
            "log": self.log[-60:], "error": self.error, "report": self.report,
        }


class JobLogHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.job: Optional[Job] = None

    def emit(self, record):
        if self.job is not None:
            try:
                self.job.on_log(record)
            except Exception:
                pass


class PageTracker:
    """Knows whether the app's page is open in a browser, so the Mac app can quit by
    itself after the page is closed (a Mac app with no window can't be reopened by
    double-clicking while it is still running).

    Pages say hello, send a heartbeat every 15 s, and say bye when closed (pagehide
    beacon). The app quits when no page is open for `grace` seconds and no report is
    running. A page that stops sending heartbeats (browser crashed) counts as closed
    after `stale` seconds - long, because browsers slow timers in background tabs to
    about once a minute. time.monotonic() does not advance while a Mac sleeps."""

    def __init__(self, grace: float = 8.0, stale: float = 150.0, startup: float = 180.0,
                 clock=time.monotonic):
        self.grace, self.stale, self.startup, self.clock = grace, stale, startup, clock
        self.pages: dict[str, float] = {}
        self.started = clock()
        self.empty_since: Optional[float] = None  # when the last page went away
        self.lock = threading.Lock()

    def hello(self) -> str:
        tab = uuid.uuid4().hex
        with self.lock:
            self.pages[tab] = self.clock()
            self.empty_since = None
        return tab

    def beat(self, tab: str) -> bool:
        with self.lock:
            if tab not in self.pages:
                return False
            self.pages[tab] = self.clock()
            return True

    def bye(self, tab: str) -> bool:
        with self.lock:
            if self.pages.pop(tab, None) is None:
                return False
            if not self.pages:
                self.empty_since = self.clock()
            return True

    def should_quit(self, busy: bool) -> bool:
        now = self.clock()
        with self.lock:
            for tab, seen in list(self.pages.items()):
                if now - seen > self.stale:
                    del self.pages[tab]
                    if not self.pages:
                        self.empty_since = seen + self.stale
            if busy or self.pages:
                return False
            if self.empty_since is None:  # no page has ever connected
                return now - self.started > self.startup
            return now - self.empty_since > self.grace


class AppState:
    def __init__(self, output_dir: Path, default_terminal: str, url_template: Optional[str],
                 browser: str, chrome_binary: Optional[str], chromedriver: Optional[str]):
        self.output_dir = output_dir
        self.default_terminal = default_terminal
        self.url_template = url_template
        self.browser, self.chrome_binary, self.chromedriver = browser, chrome_binary, chromedriver
        self.jobs: dict[str, Job] = {}
        self.current: Optional[Job] = None
        self.lock = threading.Lock()
        self.pages = PageTracker()
        self.log_handler = JobLogHandler()
        engine_log = logging.getLogger("changi")
        if engine_log.getEffectiveLevel() > logging.INFO:  # progress comes from INFO messages
            engine_log.setLevel(logging.INFO)
        engine_log.addHandler(self.log_handler)

    @property
    def busy(self) -> bool:
        return bool(self.current and self.current.state == "running")

    def start_auto_quit(self, exit_fn=None, every: float = 2.0) -> threading.Thread:
        """Background check: quit the app once its page has been closed."""
        exit_fn = exit_fn or (lambda: hard_exit(0.1))

        def loop():
            while True:
                time.sleep(every)
                if self.pages.should_quit(self.busy):
                    LOG.info("Page closed and no report running: quitting")
                    exit_fn()
                    return

        th = threading.Thread(target=loop, daemon=True, name="auto-quit")
        th.start()
        return th

    # ---- running a report --------------------------------------------------
    def start(self, terminal: str, report_date: date, show_browser: bool) -> tuple[Optional[Job], Optional[Job]]:
        """Returns (new_job, busy_job). Only one scrape runs at a time."""
        with self.lock:
            if self.current is not None and self.current.state == "running":
                return None, self.current
            job = Job(terminal, report_date, show_browser)
            self.jobs[job.id] = job
            self.current = job
        threading.Thread(target=self._run, args=(job,), daemon=True, name=f"job-{job.id}").start()
        return job, None

    def _run(self, job: Job) -> None:
        self.log_handler.job = job
        LOG.info("Report requested: Terminal %s, %s", job.terminal, job.report_date)
        cfg = s.Config(
            report_date=job.report_date, terminal=job.terminal, headless=not job.show_browser,
            output_dir=self.output_dir, doc_format="both", browser=self.browser,
            chrome_binary=self.chrome_binary, chromedriver=self.chromedriver,
            url=self.url_template.format(terminal=job.terminal) if self.url_template else None,
        )
        try:
            s.run_live(cfg)
            job.report = load_report(self.output_dir, job.terminal, job.report_date)
            job.state, job.percent, job.step = "done", 100, "Report ready"
        except s.ScraperError as exc:
            job.state, job.error = "error", friendly_error(exc, self.output_dir)
            job.step = "Could not complete the report"
        except Exception as exc:  # unexpected bug: keep the app alive, log everything
            LOG.error("Unexpected error: %s\n%s", exc, traceback.format_exc())
            job.state = "error"
            job.error = (f"Unexpected error: {exc}. Details are in the log files in "
                         f"{self.output_dir / 'logs'}")
            job.step = "Could not complete the report"
        finally:
            job.finished = time.time()
            self.log_handler.job = None
            LOG.info("Job %s finished: %s", job.id, job.state)


def friendly_error(exc: Exception, output_dir: Path) -> str:
    msg = str(exc)
    hints = []
    if isinstance(exc, s.CalendarError) and "disabled" in msg:
        hints.append("The Changi website may only offer a few days around today. Try a closer date.")
    elif "Could not start a browser" in msg:
        hints.append("Google Chrome or Microsoft Edge must be installed on this computer.")
    elif "No flight rows appeared" in msg:
        hints.append("Check the internet connection, or whether the Changi website is up.")
    elif "Terminal" in msg and "showing" in msg:
        hints.append("Try again. If it keeps happening, the website may have changed.")
    else:
        hints.append("Try again in a minute. If it keeps failing, send the files in the "
                     f"'{output_dir / 'debug'}' folder to whoever maintains this tool.")
    return f"{msg}\n\n{' '.join(hints)}"


# ----------------------------------------------------------------------------- saved reports
_REPORT_NAME = re.compile(r"^changi_t([1-4])_(\d{4}-\d{2}-\d{2})(_PARTIAL)?\.json$")


def load_report(output_dir: Path, terminal: str, report_date: date) -> Optional[dict]:
    folder = output_dir / "reports"
    for suffix in ("", "_PARTIAL"):
        p = folder / f"changi_t{terminal}_{report_date}{suffix}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    return None


def list_reports(output_dir: Path, limit: int = 60) -> list[dict]:
    folder = output_dir / "reports"
    out = []
    if folder.exists():
        for p in folder.iterdir():
            m = _REPORT_NAME.match(p.name)
            if not m:
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({
                "terminal": m.group(1), "date": m.group(2), "partial": bool(m.group(3)),
                "generated_at": data.get("generated_at", ""), "total_flights": data.get("total_flights"),
                "total_china": data.get("total_china"), "status": data.get("status", ""),
            })
    out.sort(key=lambda r: (r["date"], r["generated_at"]), reverse=True)
    return out[:limit]


# ----------------------------------------------------------------------------- HTTP
SAFE_FILE = re.compile(r"^changi_t[1-4]_[A-Za-z0-9_\-]+\.(pdf|docx|csv)$")


class Handler(BaseHTTPRequestHandler):
    server_version = f"ChangiDepartures/{APP_VERSION}"
    state: AppState  # set on the class before serving

    def log_message(self, fmt, *args):  # keep the console quiet
        LOG.debug("HTTP %s", fmt % args)

    # ---- helpers
    def _send(self, code: int, body: bytes, ctype: str, headers: Optional[dict] = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost") or self.server.allow_any_host

    # ---- GET
    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "forbidden host"})
        url = urlparse(self.path)
        st = self.state
        if url.path in ("/", "/index.html"):
            body = resource("web/index.html").read_bytes()
            return self._send(200, body, "text/html; charset=utf-8")
        if url.path == "/api/config":
            return self._json(200, {
                "app": APP_NAME, "version": APP_VERSION, "terminals": list(s.TERMINALS),
                "default_terminal": st.default_terminal, "today": s.today_in_singapore().isoformat(),
                "output_dir": str(st.output_dir),
                "busy": bool(st.current and st.current.state == "running"),
                "current_job": st.current.id if st.current and st.current.state == "running" else None,
            })
        if url.path == "/api/reports":
            return self._json(200, list_reports(st.output_dir))
        if url.path == "/api/report":
            q = parse_qs(url.query)
            try:
                term = q["terminal"][0]
                d = date.fromisoformat(q["date"][0])
                assert term in s.TERMINALS
            except (KeyError, ValueError, AssertionError):
                return self._json(400, {"error": "terminal and date are required"})
            rep = load_report(st.output_dir, term, d)
            return self._json(200 if rep else 404, rep or {"error": "no saved report"})
        if url.path.startswith("/api/job/"):
            job = st.jobs.get(url.path.rsplit("/", 1)[-1])
            return self._json(200, job.as_dict()) if job else self._json(404, {"error": "unknown job"})
        if url.path.startswith("/files/"):
            name = url.path[len("/files/"):]
            if not SAFE_FILE.match(name):
                return self._json(400, {"error": "bad file name"})
            path = st.output_dir / name
            if not path.is_file():
                return self._json(404, {"error": "file not found"})
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            disp = "inline" if name.endswith(".pdf") else "attachment"
            return self._send(200, path.read_bytes(), ctype,
                              {"Content-Disposition": f'{disp}; filename="{name}"'})
        return self._json(404, {"error": "not found"})

    # ---- POST
    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 10_000))
        st = self.state
        if not self._host_ok():
            return self._json(403, {"error": "forbidden"})
        # "bye" comes from navigator.sendBeacon when the page closes, which can't add
        # custom headers. It only works with a tab id that this page got from /api/hello
        # (other websites can't read that response), so it is safe without the header.
        if url.path == "/api/bye":
            try:
                tab = str(json.loads(raw or b"{}").get("tab", ""))
            except (ValueError, AttributeError):
                tab = ""
            return self._json(200 if st.pages.bye(tab) else 404, {})
        # A custom header can't be sent cross-site without a CORS preflight (which we
        # never approve), so other websites can't trigger runs on this computer.
        if self.headers.get("X-Requested-With") != "changi-app":
            return self._json(403, {"error": "forbidden"})
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._json(400, {"error": "invalid JSON"})
        if url.path == "/api/hello":
            return self._json(200, {"tab": st.pages.hello()})
        if url.path == "/api/heartbeat":
            ok = st.pages.beat(str(body.get("tab", "")))
            return self._json(200 if ok else 404, {"ok": ok})
        if url.path == "/api/run":
            term = str(body.get("terminal", ""))
            try:
                d = date.fromisoformat(body.get("date", ""))
            except ValueError:
                return self._json(400, {"error": "Please choose a valid date."})
            if term not in s.TERMINALS:
                return self._json(400, {"error": "Please choose a terminal."})
            job, busy = st.start(term, d, bool(body.get("show_browser")))
            if busy:
                return self._json(409, {"error": f"A report for Terminal {busy.terminal}, "
                                                 f"{busy.report_date:%d %b} is still being generated.",
                                        "job": busy.id})
            return self._json(202, {"job": job.id})
        if url.path == "/api/open-folder":
            try:
                st.output_dir.mkdir(parents=True, exist_ok=True)
                open_in_file_manager(st.output_dir)
                return self._json(200, {"ok": True, "path": str(st.output_dir)})
            except OSError as exc:
                return self._json(500, {"error": str(exc)})
        if url.path == "/api/quit":
            if st.current and st.current.state == "running" and not body.get("force"):
                return self._json(409, {"error": "A report is still being generated. Quit anyway?"})
            self._json(200, {"ok": True})
            hard_exit()
            return
        return self._json(404, {"error": "not found"})


# ----------------------------------------------------------------------------- startup
def pick_output_dir(explicit: Optional[str]) -> Path:
    """Windows: reports go next to the app; if that folder is read-only (e.g. under
    Program Files), to Documents\\ChangiReports instead.
    Mac app: always Documents/ChangiReports (an app bundle must not be written to)."""
    if explicit:
        candidates = [Path(explicit).expanduser()]
    elif IS_MAC and is_frozen():
        candidates = [documents_reports_dir()]
    else:
        candidates = [app_dir() / "ChangiReports", documents_reports_dir()]
    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            probe = c / ".write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return c
        except OSError:
            continue
    raise SystemExit(f"Cannot write reports to any of: {', '.join(map(str, candidates))}")


def already_running(port: int) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=1.5) as r:
            return json.loads(r.read()).get("app") == APP_NAME
    except Exception:
        return False


def port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def make_server(state: AppState, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"state": state})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.allow_any_host = host not in ("127.0.0.1", "localhost")
    srv.daemon_threads = True
    return srv


def say(msg: str) -> None:
    """print() that never fails (a Mac/Windows windowed app may have no console)."""
    try:
        print(msg, flush=True)
    except Exception:
        pass


def run_menu_bar(url: str, output_dir: Path) -> bool:
    """macOS: show a ✈ menu-bar item (Open / Reports folder / Quit) and run the Cocoa
    loop on the main thread. Returns False if rumps isn't available."""
    try:
        import rumps  # type: ignore
    except ImportError:
        return False

    class MenuBarApp(rumps.App):
        def __init__(self):
            super().__init__(APP_NAME, title="✈", quit_button=None)
            self.menu = [
                rumps.MenuItem("Open Changi Departures", callback=lambda _: webbrowser.open(url)),
                rumps.MenuItem("Open reports folder", callback=lambda _: open_in_file_manager(output_dir)),
                None,
                rumps.MenuItem(f"Quit {APP_NAME}", callback=self.quit_app),
            ]

        def quit_app(self, _):
            logging.shutdown()
            rumps.quit_application()

    _handle_reopen(rumps, url)
    MenuBarApp().run()
    return True


def _handle_reopen(rumps_module, url: str) -> None:
    """Double-clicking the app while it is already running makes macOS send it a
    'reopen' message instead of starting a second copy. Answer it by opening the page.
    Best effort: if this can't be set up, the app still works (auto-quit covers it)."""
    try:
        import objc  # type: ignore  (part of PyObjC, which rumps uses)
        import importlib
        impl = importlib.import_module("rumps.rumps")
        delegate_cls = getattr(impl, "NSApp")

        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, has_windows):
            webbrowser.open(url)
            return True

        objc.classAddMethods(delegate_cls, [objc.selector(
            applicationShouldHandleReopen_hasVisibleWindows_,
            selector=b"applicationShouldHandleReopen:hasVisibleWindows:", signature=b"Z@:@Z")])
    except Exception as exc:
        LOG.info("Reopen handler not installed (%s); auto-quit still applies", exc)


def main(argv=None) -> int:
    for name in ("stdout", "stderr"):  # windowed app builds can start without them
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))
    ap = argparse.ArgumentParser(description=f"{APP_NAME} desktop app")
    ap.add_argument("--terminal", choices=s.TERMINALS, default=None,
                    help="Terminal pre-selected on this desktop (managers can still change it)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--output-dir", default=None, help="Where reports are saved (default: 'ChangiReports' next to the app)")
    ap.add_argument("--no-browser", action="store_true", help="Don't open a browser tab on start")
    ap.add_argument("--browser", choices=["auto", "chrome", "edge"], default="auto",
                    help="Browser used to read the Changi website (default: Chrome, else Edge)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="Network interface. Keep 127.0.0.1 unless IT wants other PCs to reach this one.")
    ap.add_argument("--url-template", default=None, help=argparse.SUPPRESS)  # testing: page URL with {terminal}
    ap.add_argument("--chrome-binary", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--chromedriver", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--no-menu-bar", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--stay-open", action="store_true",
                    help="Mac: keep running after the page is closed (default: quit a few seconds later)")
    # Finder may add a "-psn_0_12345" argument on older macOS; ignore anything unknown.
    args, _unknown = ap.parse_known_args(argv)

    settings = {}
    for settings_file in settings_candidates():  # optional, lets IT set defaults without a shortcut
        if settings_file.exists():
            try:
                settings = json.loads(settings_file.read_text(encoding="utf-8"))
                break
            except ValueError:
                say(f"Ignoring {settings_file}: not valid JSON")
    terminal = args.terminal or str(settings.get("terminal", "1"))
    if terminal not in s.TERMINALS:
        terminal = "1"
    output_dir = pick_output_dir(args.output_dir or settings.get("output_dir"))

    url = f"http://127.0.0.1:{args.port}/?terminal={terminal}"
    if already_running(args.port):
        say(f"{APP_NAME} is already running - opening {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    if not port_free(args.port):
        say(f"Port {args.port} is used by another program. Start with --port 8766 (or another number).")
        return 1

    log_file = s.setup_logging(output_dir, "WARNING")
    state = AppState(output_dir, terminal, args.url_template, args.browser,
                     args.chrome_binary, args.chromedriver)
    server = make_server(state, args.port, args.host)
    say("=" * 64)
    say(f" {APP_NAME} is running.")
    say(f" Open:     {url}")
    say(f" Reports:  {output_dir}")
    say(f" Log:      {log_file}")
    say(" Keep this window open while using the page. Close it (or press Quit app on the page) to stop.")
    say("=" * 64)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    server_thread = threading.Thread(target=server.serve_forever, daemon=True, name="http")
    server_thread.start()
    # Mac: an app without a window can't be reopened by double-clicking while it is still
    # running, so quit once the page has been closed (unless told to stay open).
    if IS_MAC and not args.stay_open and settings.get("auto_quit", True):
        state.start_auto_quit()
    try:
        if IS_MAC and not args.no_menu_bar and run_menu_bar(url, output_dir):
            pass  # menu-bar loop returned: user chose Quit
        else:
            while server_thread.is_alive():
                server_thread.join(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
