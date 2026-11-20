# Changi Departures

A small desktop app for store managers. It shows how many flights leave Changi
Airport for each destination country in every 2-hour block, from 06:00 to 02:00
the next day. It covers **any terminal (T1–T4)** and gives the number of flights
to mainland China for each block.

The app runs on the store's own computer and opens as a normal web page in the
browser. Behind the scenes it reads the public Changi departures page using
Chrome, or Microsoft Edge if Chrome isn't installed.

---

## For store managers: using the app

1. Open the app.
   - **Windows:** double-click **ChangiDepartures** (desktop shortcut). A black
     window opens, and then the page opens in your browser. **Keep the black
     window open**; closing it stops the app.
   - **Mac:** open **Changi Departures** from Applications. The page opens in
     your browser and a **✈** appears in the menu bar at the top of the screen.
     **Closing the page closes the app** a few seconds later (after any report
     that is still running finishes), so opening it from Applications always
     works again. While it is running, ✈ reopens the page or the reports folder.

   On either computer, **Quit app** at the top of the page also stops it.
2. Choose the **terminal** (T1–T4) and the **date**. *Today* and *Tomorrow* are
   one click.
3. Press **Generate report**. This takes about 1–3 minutes, and a progress bar
   shows what is happening.
4. The report shows on screen:
   - total departures, flights to China, and the busiest 2 hours;
   - one row per 2-hour block, with the number of flights, flights to China,
     and the country codes (hover a code to see the country name);
   - **Open PDF**, **Word**, **Flight list (CSV)** and **Print** buttons.
5. Reports are saved. Any report generated before appears under **Saved
   reports** and opens instantly. Press *Generate report* again for the latest
   flight times, which change during the day as flights are delayed or retimed.

If something goes wrong, a red box explains what happened. Try again after a
minute. If it keeps failing, tell the person who maintains the tool.

---

## For whoever sets it up

### Easiest — let GitHub build the apps (no Mac or Windows PC needed)

The project includes a GitHub build (`.github/workflows/build.yml`). Each time
a change is pushed to the repository, GitHub builds the app on its own
computers:
- a Mac with Apple silicon (M1 or later);
- an older Intel Mac;
- a Windows PC.

It then checks that each app starts and publishes the three zips under
**Releases** on the repository page, newest at the top. A run takes about
10–15 minutes. To rebuild without changing anything, go to the **Actions** tab,
choose **Build apps**, and press **Run workflow**.

Send each store the right zip:

| Store computer | Zip |
|---|---|
| Mac with Apple silicon (M1 or later) | `Changi-Departures-Mac-Apple-silicon.zip` |
| Older Intel Mac | `Changi-Departures-Mac-Intel.zip` |
| Windows PC | `Changi-Departures-Windows.zip` |

Then follow steps 2–4 of the matching option below. The Mac `xattr` step is
still needed, because the app isn't Apple-signed.

**Cost:** a private repository on a free GitHub account gets a monthly
allowance of build time. Mac time counts 10× and Windows 2×, so the allowance
covers roughly 10 full builds a month. Unless you add a payment method,
GitHub doesn't charge for going over; builds just pause until the next month.
Public repositories build for free without limit, but then anyone can see the
code.

### Option 1 — packaged app (recommended for store desktops; no Python needed)

1. On **one** Windows PC that has **Python 3.12**, double-click `build_exe.bat`.
   It installs everything it needs and builds the folder
   `dist\ChangiDepartures` (about 5 minutes).
2. Copy that whole folder to each store desktop, for example to
   `C:\ChangiDepartures`. Then right-click `ChangiDepartures.exe` and choose
   *Send to → Desktop (create shortcut)*.
3. Set each store's terminal in `settings.json` inside the folder:
   ```json
   { "terminal": "3" }
   ```
   Managers can still switch terminals in the page. The last terminal used is
   remembered on that computer.
4. The desktop needs **Google Chrome or Microsoft Edge** and internet access to
   changiairport.com. The first run downloads a small browser driver, which
   Selenium does automatically. If your IT blocks that download, see
   *Limitations* below.

Reports are saved in `ChangiReports` inside the app folder. If that folder is
read-only, for example under Program Files, they go to `Documents\ChangiReports`
instead. **Open reports folder** in the app opens it.

### Option 2 — run with Python

Double-click `Start Changi Departures.bat`. The first run creates a private
Python environment and installs the libraries. You can also run it from a
terminal:

```powershell
python app.py                 # opens the page
python app.py --terminal 3    # page defaults to Terminal 3
```

### Option 3 — Mac app

The Mac app has to be **built on a Mac**, in the same way the Windows `.exe` is
built on Windows.

1. On a Mac with **Python 3.10 or newer from python.org** (3.15 works),
   double-click `build_mac.command`. The Python that comes with macOS can't
   build apps, so the script skips it. The first time, the script also asks
   you to install Apple's free **Command Line Tools**, which contain `lipo` and
   other tools needed to build a Mac app. Click *Install*, wait for it to finish
   (about 5–15 minutes), then run the script again. If macOS won't open it, right-click it and choose *Open*,
   or run `bash build_mac.command` in Terminal. It takes about 5 minutes and
   produces:
   - `dist/Changi Departures.app`, the app;
   - `dist/Changi Departures (Mac).zip`, the same app zipped for sending.
     Always share this zip rather than copying the `.app` loose.

   `changi_departures.spec` is only the build recipe. Each build:
   - starts from a clean setup;
   - tests that the new app starts;
   - saves everything it printed to `build_log.txt`.

   If the build fails, the message says at which step, and `build_log.txt` is
   the file to send for help.
2. **Send only `Changi Departures (Mac).zip`** to the other Macs (AirDrop works).
   Don't send the whole `dist` folder; `dist/ChangiDepartures` is a build
   leftover. On each Mac, double-click the zip in Downloads to unzip it, then
   drag **Changi Departures.app** into **Applications**.
3. **On each Mac that received it, once per new version**, macOS says the app
   "is damaged". It isn't: the app isn't signed by an Apple-registered
   developer, and macOS distrusts it because it came from another computer.
   Open **Terminal**, paste this and press Return:
   ```
   xattr -cr "/Applications/Changi Departures.app"
   ```
   It prints nothing when it works. If it says *Permission denied*, put
   `sudo ` in front and enter the Mac's login password. The Mac it was built on
   doesn't need this step. Signing and notarising the app with an Apple
   Developer account (US$99 a year) would remove the warning altogether.
4. Reports are saved in **Documents → ChangiReports**. To preset a store's
   terminal, put `settings.json` there containing `{ "terminal": "3" }`.
5. The Mac needs **Google Chrome or Microsoft Edge**. Safari can't be used
   because it doesn't support the browser controls the scraper relies on.

An app built on an Apple-silicon Mac (M1 or later) runs on Apple-silicon Macs.
Build on an Intel Mac for Intel Macs.

To run it with Python instead, double-click `Start Changi Departures (Mac).command`.

### App options

| Option | Meaning |
|---|---|
| `--terminal N` | Terminal pre-selected on this computer (overrides `settings.json`) |
| `--port 8765` | Local port. The page is `http://127.0.0.1:8765` |
| `--output-dir PATH` | Where reports are saved |
| `--browser auto\|chrome\|edge` | Browser used to read the Changi site |
| `--no-browser` | Start without opening a browser tab |
| `--host 127.0.0.1` | Keep the default. The app only accepts connections from the same computer |
| `--stay-open` | Mac: keep running after the page is closed (or `"auto_quit": false` in `settings.json`) |

`settings.json` can also hold `"output_dir"`. Starting the app a second time
just opens the page that is already running, so only one copy runs. Only one
report is generated at a time.

### How it fits together

| File | Role |
|---|---|
| `app.py` | Local web server (Python standard library only): runs reports in the background, serves the page and the files |
| `web/index.html` | The page managers use, a single file that works offline |
| `changi_scraper.py` | The scraper and report engine; also usable from the command line (below) |
| `build_exe.bat`, `Start Changi Departures.bat` | Windows packaging and launching |
| `build_mac.command`, `Start Changi Departures (Mac).command` | Mac packaging and launching |
| `changi_departures.spec` | The build recipe, shared by the Mac build, the Windows build and GitHub (only the recipe, not the app) |
| `.github/workflows/build.yml` | The GitHub build: tests, then the Mac (×2) and Windows apps, published as a release |
| `settings.example.json` | Example per-store settings (`terminal`, `output_dir`) |

---

## A. Command line (without the app)

```powershell
python changi_scraper.py                         # today (Singapore), headless Chrome
python changi_scraper.py --date 2026-10-14       # a specific date
python changi_scraper.py --date 2026-10-14 --visible   # watch it work
```

The **Run and Debug** panel in VS Code also has three ready-made configurations
(from `.vscode/launch.json`): *today*, *pick a date*, and *inspect live DOM*.

| Option | Default | Meaning |
|---|---|---|
| `--date YYYY-MM-DD` | today in SGT | Report date |
| `--terminal 1\|2\|3\|4` | `1` | Changi terminal |
| `--browser auto\|chrome\|edge` | `auto` | Chrome if installed, otherwise Microsoft Edge |
| `--visible` | off | Show the Chrome window |
| `--output-dir DIR` | `output` | Where CSVs, raw text, logs and debug files go |
| `--cancelled exclude\|include` | `exclude` | Count cancelled flights? They stay in the CSV either way (`counted=no`) |
| `--time-basis scheduled\|estimated` | `scheduled` | Bucket by scheduled time, or by estimated/retimed time when one is shown |
| `--codeshare auto\|strict` | `auto` | How codeshares are merged (see §5) |
| `--doc-format both\|docx\|pdf\|none` | `both` | Which readable report documents to write |
| `--allow-partial` | off | Write `*_PARTIAL.csv` instead of failing when data is incomplete |
| `--inspect` | off | Save DOM diagnostics and exit (no report) |
| `--replay-raw DIR` | — | Re-parse saved `raw_listing_*.txt` files without a browser |
| `--timeout N` | 45 | Seconds to wait for the listing to appear |
| `--log-level` | INFO | Console verbosity. The log file is always written at DEBUG |

Exit codes: `0` complete, `2` scrape failed (no report written), `3` partial
report written.

## B. Output files

With the command line, everything goes into `output/`. The app uses its `ChangiReports` folder instead. `N` is the terminal number.

| File | Contents |
|---|---|
| `changi_tN_departures_YYYY-MM-DD.csv` | One row per physical departure in the window, including excluded ones (cancelled, wrong terminal), with the reason |
| `changi_tN_country_summary_YYYY-MM-DD.csv` | `Time Interval, Country Code, Flights` |
| `changi_tN_report_YYYY-MM-DD.pdf` / `.docx` | **One-page readable report**: date at the top; one row per 2-hour interval with total flights, flights to China and the country codes with counts; total row and overall totals at the bottom |
| `raw/changi_tN_raw_listing_YYYY-MM-DD.txt` | Captured listing text for each scraped date, with row markers |
| `logs/run_*.log` | Full DEBUG log |
| `debug/` | Screenshots, HTML, page text and DOM reports from failures; `all_parsed_*.csv` holds every parsed row, including those outside the window |

"To China" counts mainland China (`CN`) only. Hong Kong (`HK`), Macau (`MO`)
and Taiwan (`TW`) appear as separate countries. To count them as China too,
change `CHINA_CODE` handling in `build_report_table`. If the Word or PDF file
is open when you re-run, close it first, because Windows locks open files.
A partial run prints a red **DATA INCOMPLETE** line at the top of the documents.

The CSVs are UTF-8 with a BOM, so Excel shows the "–" in interval labels correctly.

### Example terminal output — ILLUSTRATIVE DATA

This output came from a run against the **mock page**, which has a small,
made-up schedule. It is not real Changi data.

```
==================================================================
Changi Airport Terminal 1 — departures by destination country
Report date : Thu 31 Dec 2026
Window      : 31 Dec 06:00 → 01 Jan 02:00 (Singapore time, end exclusive)
Counting    : scheduled time; cancelled excluded; codeshares merged (auto)
Data status : COMPLETE
==================================================================

06:00–08:00  (Thu 31 Dec)  —  3 flight(s)
    ID: 1
    MY: 1
    TH: 1

08:00–10:00  (Thu 31 Dec)  —  2 flight(s)
    JP: 1
    TH: 1

10:00–12:00  (Thu 31 Dec)  —  0 flight(s)
    (none)
...
22:00–00:00  (Thu 31 Dec)  —  2 flight(s)
    TW: 1
    US: 1

00:00–02:00  (Fri 01 Jan)  —  3 flight(s)
    AU: 2
    KR: 1

------------------------------------------------------------------
TOTAL 31 Dec 06:00 – 01 Jan 02:00: 16 flight(s)
Excluded in window: 1 cancelled
Unknown destinations (counted as '??'): Atlantis (QQQ) ×1
```

### CSV examples — ILLUSTRATIVE DATA (same mock run)

`changi_t1_country_summary_2026-12-31.csv`
```
Time Interval,Country Code,Flights
06:00–08:00,ID,1
06:00–08:00,MY,1
06:00–08:00,TH,1
08:00–10:00,JP,1
08:00–10:00,TH,1
...
00:00–02:00,AU,2
00:00–02:00,KR,1
```

`changi_t1_departures_2026-12-31.csv` (selected columns)
```
scheduled_date,scheduled_time,time_interval,flight_number,codeshare_flight_numbers,airline,destination_city,destination_iata,destination_country,status,counted,exclusion_reason
2026-12-31,06:00,06:00–08:00,TR416,,Scoot,Kota Kinabalu,BKI,MY,Scheduled,yes,
2026-12-31,09:30,08:00–10:00,SQ106,NH6254,Singapore Airlines,Tokyo,NRT,JP,Scheduled,yes,
2026-12-31,11:15,10:00–12:00,MH604,,Malaysia Airlines,Kuala Lumpur,KUL,MY,Cancelled,no,cancelled
2027-01-01,00:00,00:00–02:00,TR500,,Scoot,Perth,PER,AU,Scheduled,yes,
```

## C. How the calendar is handled

The script uses **no CSS class names or fixed XPaths** for the site. The live
DOM could not be inspected, so guessing selectors would have meant presenting
inventions as facts. Instead, every element is identified by what it shows and
how it behaves. All of this lives in one JavaScript helper (`JS_LIB`). It walks
**open shadow roots** and also searches same-origin **iframes** if nothing is
found in the main page.

1. **Trigger.** The script looks for visible elements whose own text is a full
   date (e.g. `Sat, 10 Oct 2026`), whose `aria-label`, `title` or `placeholder`
   mentions *date* or *calendar*, or which are an `<input type="date">`.
   Candidates are **rejected** if they sit inside a link to another page, or
   inside a flight row (any nearby ancestor containing `City (XXX)`). This
   rejection fixes the old failure, where the scraper clicked a flight and
   landed on its details page. A month-and-year label alone (`SEP 2026`) never
   counts as a trigger.
2. **Calendar.** After the click, the script waits for a visible month header
   (`OCT 2026`, `October 2026`, …). The header's calendar is the nearest
   ancestor containing **28 or more** day-number cells. Day cells can be any
   element (`div`, `span`, `td`, `button`); the script doesn't care which.
3. **Month navigation.** The script works out how many months to move,
   including across years. It finds the arrows first by `aria-label` or title
   (*next*, *previous*), and otherwise by geometry: clickable or icon elements
   on the header's row, where the leftmost is *previous* and the rightmost is
   *next*. After each click it checks that the header changed. If the header
   doesn't change, the site probably doesn't offer that month, and the script
   stops with an error.
4. **Day cell.** Grids often show overflow days (`28 29 30 | 1 2 … 31 | 1 2`).
   The script finds the unbroken run `1, 2, …, N`, where N is the number of
   days in the month, and picks the cell inside that run. A disabled cell
   produces a clear error rather than a silent no-op.
5. **Clicking.** The script uses a real mouse click when the target is
   unobstructed. If another element covers it, the script dispatches the
   pointer and mouse events directly to the target, so a covering link can't
   be clicked by accident.
6. **Navigation guard.** After *every* click, the script checks that the URL
   is still the departures listing and that no new tab opened. If either check
   fails, it goes back, raises `PageNavigationError`, and saves debug files.
7. **Verification.** The script re-reads the displayed date until it equals
   the target. If the page doesn't show a readable date, it reopens the
   calendar and checks that the target cell is marked selected. If neither
   works, it raises `CalendarError` and saves a screenshot, HTML and a DOM
   report.
8. **Refresh.** The script waits for the old rows to be replaced or the
   listing content to change, and then for the listing to stay unchanged
   across three consecutive checks.

If the page already shows the requested date, the calendar is skipped.

## D. Counting rules

**Next-day flights.** The script uses timezone-aware datetimes (`Asia/Singapore`)
and real calendar dates throughout. The run scrapes two pages:

- **Date D:** only rows dated D are kept. The 06:00–23:59 rows are used, and
  D's own 00:00–05:59 rows fall outside the window.
- **Date D+1, selected in the calendar:** only rows dated D+1 are kept, and
  only 00:00–01:59 falls inside the window.

The window is half-open, `[D 06:00, D+1 02:00)`. That means 06:00, 23:59,
00:00 (D+1) and 01:59 (D+1) are included, while 02:00 (D+1) and 00:00–01:59
on D itself are excluded. Month and year rollovers (30 Sep → 1 Oct,
31 Dec → 1 Jan) come from ordinary date arithmetic, so no special cases are
needed. Each row's date is assigned in this order:

1. Explicit date separators on the page, if there are any.
2. Otherwise, the selected date. A drop of more than 6 hours in the
   chronological times is treated as midnight.
3. If midnight was only inferred, the largest same-day block is anchored to
   the selected date. This guards against "earlier" rows from the previous
   day. A `(+1)` suffix next to a time is honoured.

**Loading all flights.** *SHOW EARLIER FLIGHTS* is clicked until the earliest
row covers the needed start time or the control disappears. *SHOW LATER
FLIGHTS* is then clicked the same way, up to the needed end time. Two clicks in
a row that add nothing count as exhausted, and there is a hard cap of 40 clicks.
Coverage is checked for both pages. If it can't be confirmed, the run fails, or
is marked PARTIAL with `--allow-partial`.

**Parsing.** Rows are found structurally: the smallest element containing a
`City (IATA)` text, an `HH:MM` time and a flight number. Each row's text is
parsed on its own, so a status like *Delayed* or *Est 18:45* stays with the
right flight. Text outside detected rows is parsed with the original script's
sequential approach, which also picks up date separators. The parser handles:

- `TR416Scoot`, `TR416 · Scoot`, `TR 416 Scoot`, and several flight numbers on one line.
- An airline name on its own line, and `17:15TR416Scoot` on one line.
- `Est`, `Retimed`, `Departed` times, cancelled flights, `via` stops, and terminal, gate and check-in lines.
- Gate codes such as `C22` and `D41`, which are never mistaken for flight numbers.

Rows with a flight number but no parseable destination are logged as
**unparsed** and flagged in the report. They are never silently dropped.

**Codeshares and duplicates.** Rows that share a flight number at the same
scheduled time are always merged. In `auto` mode, which is the default, rows
are also merged when they have the same scheduled minute and destination,
*different* carriers, no conflicting gate or check-in row, and the same
cancelled state. The first row is kept as primary, and the others are listed in
`codeshare_flight_numbers`. Every merge is logged. Use `--codeshare strict` if
you see over-merging.

**Cancelled and retimed.** Cancelled flights are excluded by default
(`--cancelled include` to count them). Flights are bucketed by **scheduled**
time unless you pass `--time-basis estimated`.

**Countries.** The destination **IATA code** is looked up in the `airportsdata`
reference dataset (about 8,000 airports). If that fails, a built-in IATA table
of about 200 airports served from SIN is tried, then a city/alias table that
handles accents, case, airport names and alternative spellings. Unknown
destinations are counted as `??` and listed at the end of the report. Three-letter
airport codes and two-letter country codes are kept in separate fields. A
destination of `SIN` is treated as not-a-departure and excluded.

The report checks that the sum of the country counts equals the number of
counted flights.

## E. Tests

```powershell
python -m pytest -m "not browser and not live"   # 94 offline unit tests (no browser, no network)
python -m pytest -m browser                      # real Chrome vs the local mock page + the app UI (no network)
$env:CHANGI_LIVE="1"; python -m pytest -m live -s  # real changiairport.com
```

- **Unit tests** (`tests/test_unit.py`) cover:
  - interval boundaries: 06:00, 23:59, D+1 00:00 and 01:59, the 02:00 exclusion, and D's own 00:00–01:59 excluded;
  - month, year and leap-day transitions;
  - date-text parsing, flight, time and gate parsing, and row and free-text parsing;
  - midnight inference, country mapping, unknown destinations, dedupe and codeshares, and cancelled and retimed policies;
  - totals and CSV formats;
  - rejection of a listing that shows the wrong date.
- **Browser tests** (`tests/test_browser_mock.py`) drive Chrome through the mock.
  The mock's calendar sits in a shadow DOM, uses div day cells and unlabeled
  SVG arrows, and has rows that are links to a details page plus a decoy dated
  link. The tests run:
  - full runs for 10 Oct, 30 Sep → 1 Oct and 31 Dec → 1 Jan;
  - an unselectable date, which must fail and save a screenshot;
  - `--inspect`.
- **Live test** (`tests/test_live_site.py`) is skipped unless `CHANGI_LIVE=1` is set.

## F. Debugging (do this on the first live run)

1. Run `python changi_scraper.py --inspect --visible --log-level DEBUG`.
   It opens the page, opens the calendar and writes these files:
   - `output/debug/inspect_report.json`, which records:
     - shadow roots and iframes found;
     - date-trigger candidates with scores and descriptions;
     - calendar headers and day-cell counts;
     - which arrows were detected and how;
     - the number of rows plus five sample row texts;
     - the "show earlier/later" controls;
     - the calendar's HTML.
   - Screenshots and HTML of the page and of the open calendar.
2. Check these things in the report:
   - `rowCount` > 0 and `sampleRows` look like flights. This shows that rows are detected.
   - `dateTriggers[0]` is the real date control. This shows that the right trigger was found.
   - `calendar_opened: true`, and `calendar_arrows.next` and `prev` are the arrow buttons.
3. Run a normal report with `--visible`. If it fails, the error names the step,
   and `output/debug/` holds a screenshot, HTML, page text and DOM report from
   the moment of failure.
4. Fix parsing without a browser. Edit the parser, then re-run it on the saved
   text with `python changi_scraper.py --replay-raw output/raw --date 2026-10-14`.
5. If something needs adapting, the most useful things to share are:
   - `inspect_report.json`
   - the two `raw_listing_*.txt` files
   - the failure screenshot

   These are what the heuristics in `JS_LIB` and the regexes would be tuned against.

## G. Live-site findings (first live run, 20 Oct 2026)

From the first live run's debug files:

- **Two date boxes on the page.**
  - The header has a site-wide search widget whose date box always shows *today*.
  - The listing has its own "Pick a date" box.

  The scraper now picks the date box that sits closest to the flight rows in the
  page structure, with extra weight for a nearby "date" label. The header box
  is ignored.
- **Selecting a date works.** The site also writes the date into the URL
  (`…departures.html?terminal=1&date=2026-10-21`).
- **Each flight row is a link** to `flight-detail.html?flight_id=ZG54_20261021`.
  The date in that link is now the **authoritative date for each row**.
  Guessing midnight from the times is only a fallback.
- **The date check uses several signals.** The listing's date box must show the
  target date, and the row link dates must not show a different day for most
  rows. The URL `date=` is used only when the box is unreadable.
- **New text formats are handled:**
  - `Tokyo (Narita) (NRT)`;
  - `GATE INFO AT 20:30`, which gives the time the gate is announced, not a departure time;
  - `Codeshare` followed by run-together numbers (`QF8353TP6356`, `6E3814AA8488…`);
  - `Check-In Row 7`;
  - `via Taipei`.
- **The cookie banner** ("Continue") is now dismissed.

`tests/fixtures/live_departures_2026-10-21.html` is a static copy of that page
with scripts and styles removed, and the browser tests run against it.

## H. Limitations (still unverified on the live site)

- Month navigation in the live calendar (arrows and day cells) has not been
  exercised yet, because the first run only moved one day ahead within October.

- **All live-DOM detection is heuristic and unverified:** the date trigger,
  calendar header, arrows, day cells, row containers and the show-more
  controls. The heuristics were chosen to survive many plausible layouts and
  are proven on a deliberately awkward mock. The real site could still differ.
  Some things that would break a heuristic:
  - **Closed shadow roots and cross-origin iframes** can't be read.
  - **Times shown in a 12-hour format** (e.g. `5:15 PM`). The parser expects 24-hour `HH:MM`.
  - **A flight number and city on one line** without `(IATA)` brackets.
  - **A calendar showing more than 7 weeks.**
- **The site may limit selectable dates**, for example to a few days around
  today. Choosing a date outside that range fails with a clear error. It does
  not fall back to another date.
- **Codeshare detection in `auto` mode is inferred.** It assumes rows with the
  same minute and destination are one aircraft. Two genuinely separate flights
  by different airlines to the same airport at the same minute, with no gate
  or check-in data to tell them apart, would be merged. Use `--codeshare strict`
  if the logged merges look wrong. The **operating** airline is taken from an
  "Operated by" line when the site shows one; otherwise it is the first-listed
  carrier.
- **Bot protection** (CAPTCHAs or access-denied pages) isn't bypassed. The run
  then fails at "no flight rows appeared" with a screenshot of what loaded.
- **Midnight detection without date separators** assumes the listing is sorted
  by time and that no gap exceeds 6 hours.
- **Terminals other than T1 haven't been run on the live site yet.** The app
  assumes the site handles `?terminal=3` the same way it handles `?terminal=1`.
  To guard against that being wrong, every run checks the terminal printed on
  each flight row. If the page shows a different terminal, the run stops with a
  clear error rather than producing a wrong report.
- **The packaged app was built and tested on Linux with the same PyInstaller
  settings, not on Windows.** The Linux build was tested end to end: Terminal 3
  report, PDF, Word and CSV. Do one test report after the first Windows build.
  The Edge fallback hasn't been exercised yet either.
- **The Mac app hasn't been built or opened on a real Mac yet.** I wrote and
  tested it without access to a Mac. What was tested on Linux, with macOS
  simulated where the code differs:
  - the same build recipe, built with PyInstaller;
  - a full Terminal 3 report from the built app;
  - Quit, and where files are saved.

  Still to check on a Mac:
  - the `.app` wrapper itself;
  - the ✈ menu-bar icon (it uses the `rumps` library, and without it the app
    still works, just with no icon). On a MacBook with a notch, the icon can be
    hidden when the menu bar is crowded;
  - double-clicking the app while it's already running should open the page.
    That part is Mac-only code I couldn't run. If it doesn't work, closing
    the page still quits the app, so the next double-click starts it fresh.

  Build it once and run one report before handing it out.
- **Work-PC restrictions.**
  - On first use, Selenium downloads a driver matching the installed Chrome or
    Edge. If IT blocks that download, place `chromedriver.exe` in the app
    folder and start the app with `--chromedriver chromedriver.exe`.
  - Antivirus may question an unsigned `.exe`. Ask IT to allow the
    `ChangiDepartures` folder.
