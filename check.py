#!/usr/bin/env python3
"""Check a Goodreads user's public pages and email any new activity.

Goodreads no longer serves the updates RSS feed (it returns 404), and the
shelf lists need a login. What is public without signing in:
  * the status-updates page  (/user_status/list/<id>)  - progress updates + her text
  * the profile page         (/user/show/<id>)          - currently-reading books
                                                          (with authors) and shelf counts
So this script reads those two pages and compares them with state.json.

What it can report:
  * new progress updates (page N of M / N% done, with her status text)
  * a book newly on her currently-reading shelf          -> "Started reading"
  * a book leaving currently-reading while `read` grows   -> "Finished reading"
  * ... while `did-not-finish` grows                       -> "Marked did-not-finish"
  * to-read / read counts going up                        -> "Added N books to to-read" etc.
    (the titles aren't public without a login)
Ratings and review text are not visible signed out, so they can't be reported.

Settings come from environment variables (GitHub secrets in Actions). When
run locally, they're also read from a git-ignored .env file next to this
script (see .env.example); real environment variables win.
  FRIEND_NAME         first name used in the email subject and greeting
  PROFILE_URL         https://www.goodreads.com/user/show/<id>-<name>
  GMAIL_ADDRESS       Gmail account that sends the email
  GMAIL_APP_PASSWORD  16-character Gmail app password
  ALERT_TO            address that receives the email (defaults to GMAIL_ADDRESS)
  DRY_RUN             "true" behaves like --dry-run
  STATE_FILE          where seen items are kept (default: state.json, git-ignored)

In GitHub Actions (GITHUB_ACTIONS=true) the log is public for a public repo,
so the script only logs counts there, never book titles or her status text.

Flags:
  --dry-run              print the email instead of sending it; don't save state
  --status-file PATH     read the status page from a local file
  --profile-file PATH    read the profile page from a local file
  --now ISO              pretend current time (for testing)
"""

import argparse
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

CENTRAL = ZoneInfo("America/Chicago")
GOODREADS_TZ = ZoneInfo("America/Los_Angeles")   # signed-out pages show Pacific time
HERE = Path(__file__).resolve().parent
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"


def load_dotenv(path: Path):
    """Minimal .env reader: KEY=VALUE lines; existing environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(HERE / ".env")
STATE_FILE = HERE / os.environ.get("STATE_FILE", "state.json")
FAIL_ALERT_AFTER = 6          # consecutive failed runs before a "check failing" email
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


# ---------------------------------------------------------------- fetching
def fetch(url: str) -> str:
    # The cache-buster makes sure we never get a stale cached copy.
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(f"{url}{sep}nocache={int(time.time())}", headers={
        "User-Agent": UA,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        if "sign_in" in resp.geturl():
            raise ValueError("redirected to the sign-in page")
        return resp.read().decode("utf-8", "replace")


def clean(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("‎", "")
    s = re.sub(r"[ \t\r]+", " ", s)
    return re.sub(r"\s*\n\s*", "\n", s).strip()


def short_title(t: str) -> str:
    """'Tough Guy (Game Changers, #3)' -> 'Tough Guy'"""
    return re.sub(r"\s*\([^()]*#[\d.]+\)\s*$", "", t).strip()


def parse_timestamp(text: str, now: datetime):
    text = text.strip()
    try:
        return datetime.strptime(text, "%b %d, %Y %I:%M%p").replace(tzinfo=GOODREADS_TZ)
    except ValueError:
        pass
    if "ago" in text:
        if "less than" in text:
            return now
        delta = timedelta()
        for n, unit in re.findall(r"(\d+)\s*(day|hour|min|sec)", text):
            delta += {"day": timedelta(days=int(n)), "hour": timedelta(hours=int(n)),
                      "min": timedelta(minutes=int(n)), "sec": timedelta(seconds=int(n))}[unit]
        return now - delta
    return None


# ---------------------------------------------------------------- page parsing
def parse_statuses(page: str, now: datetime):
    """Status updates, newest first."""
    statuses = []
    chunks = re.split(r'<div id="user_status_(\d+)">', page)
    for sid, chunk in zip(chunks[1::2], chunks[2::2]):
        header = re.search(r'user_status_header">(.*?)</span>', chunk, re.S)
        header_html = header.group(1) if header else ""
        # Drop the leading "<a>Her Name</a>".
        action_html = re.sub(r"^\s*<a [^>]*>[^<]*</a>", "", header_html, count=1)
        book = re.search(r'<a href="[^"]*/book/show/(\d+)[^"]*"[^>]*>([^<]+)</a>', action_html)
        body = re.search(r'<div class="readable body">(.*?)</div>', chunk, re.S)
        stamp = re.search(rf'href="/user_status/show/{sid}">([^<]+)</a>', chunk)
        statuses.append({
            "id": sid,
            "action": clean(action_html),
            "book_id": book.group(1) if book else "",
            "book": short_title(html.unescape(book.group(2))) if book else "",
            "text": clean(body.group(1)) if body else "",
            "when": parse_timestamp(stamp.group(1), now) if stamp else None,
        })
    return statuses


def parse_profile(page: str, now: datetime):
    """Currently-reading books {book_id: {...}} and shelf counts {shelf: n}."""
    shelves = {}
    for shelf, n in re.findall(
            r'userShowPageShelfListItem" href="[^"]*[?&]shelf=([^"&]+)"[^>]*>[^<]*?\((\d+)\)', page):
        shelves[html.unescape(shelf)] = int(n)

    reading = {}
    box = re.search(r"is Currently Reading</a></h2>(.*?)(?:<div class=\"[^\"]*bigBox\"|$)", page, re.S)
    if box:
        for chunk in re.split(r'<div class="Updates', box.group(1))[1:]:
            m = re.search(r'class="bookTitle" href="/book/show/(\d+)[^"]*">([^<]+)</a>', chunk)
            if not m:
                continue
            author = re.search(r'class="authorName" href="[^"]*">([^<]+)</a>', chunk)
            stamp = re.search(r'class="updatedTimestamp"[^>]*>([^<]+)</a>', chunk)
            reading[m.group(1)] = {
                "title": short_title(html.unescape(m.group(2))),
                "author": html.unescape(author.group(1)).strip() if author else "",
                "when": parse_timestamp(stamp.group(1), now) if stamp else None,
            }
    return reading, shelves


# ---------------------------------------------------------------- formatting
def when_phrase(dt, now):
    if dt is None:
        return "recently"
    dt = dt.astimezone(CENTRAL)
    today = now.date()
    if dt.date() == today:
        if dt.hour < 5:
            return "last night"
        if dt.hour < 12:
            return "this morning"
        if dt.hour < 17:
            return "this afternoon"
        return "this evening"
    if dt.date() == today - timedelta(days=1):
        return "last night" if dt.hour >= 20 else "yesterday"
    return f"{dt.strftime('%b').lower()} {dt.day}"


def book_line(title, author):
    if not title:
        return ""
    return f"* {title} by {author}" if author else f"* {title}"


def describe_status(s, authors):
    """One status update -> (book_line, [detail lines])."""
    a = s["action"]
    details = []
    if m := re.search(r"is on page (\d+) of (\d+)", a, re.I):
        page, total = int(m.group(1)), int(m.group(2))
        details.append(f"* Progress update: page {page} of {total} (~{round(page * 100 / total)}%)")
    elif m := re.search(r"is (\d+)% done", a, re.I):
        details.append(f"* Progress update: {m.group(1)}% done")
    elif re.search(r"finished", a, re.I):
        details.append("* Finished reading")
    else:
        details.append(f"* {a[:1].upper()}{a[1:]}")
    if s["text"]:
        details.append(f'* Her status: "{s["text"]}"')
    return book_line(s["book"], authors.get(s["book_id"], "")), details


def plural(n, word):
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def find_events(state, statuses, reading, shelves, now):
    """Everything new since the last run, as (when, book_line, details)."""
    events = []
    authors = {**state.get("authors", {}),
               **{bid: b["author"] for bid, b in reading.items() if b["author"]}}

    seen = set(state.get("seen_status", []))
    for s in statuses:
        if s["id"] not in seen:
            events.append((s["when"],) + describe_status(s, authors))

    old_reading = state.get("reading", {})
    old_shelves = state.get("shelves", {})
    grew = {k: shelves.get(k, 0) - old_shelves.get(k, 0) for k in shelves}

    for bid, b in reading.items():
        if bid not in old_reading:
            # The shelf timestamp can be old (e.g. a re-read), so only trust a recent one.
            when = b["when"] if b["when"] and now - b["when"] < timedelta(days=2) else now
            events.append((when, book_line(b["title"], b["author"]), ["* Started reading"]))

    finished_named = dnf_named = 0
    for bid, b in old_reading.items():
        if bid in reading:
            continue
        line = book_line(b["title"], b.get("author", ""))
        if grew.get("read", 0) > finished_named:
            finished_named += 1
            events.append((now, line, ["* Finished reading"]))
        elif grew.get("did-not-finish", 0) > dnf_named:
            dnf_named += 1
            events.append((now, line, ["* Marked did-not-finish"]))
        else:
            events.append((now, line, ["* Took it off currently-reading"]))

    extra = []
    if (n := grew.get("to-read", 0)) > 0:
        extra.append(f"* Added {plural(n, 'book')} to to-read")
    if (n := grew.get("read", 0) - finished_named) > 0:
        extra.append(f"* Marked {plural(n, 'more book')} as read")
    if (n := grew.get("did-not-finish", 0) - dnf_named) > 0:
        extra.append(f"* Marked {plural(n, 'more book')} did-not-finish")
    if extra:
        events.append((now, "", extra))
    return events


def build_email(events, friend, profile_url, now):
    blocks, last_when = [], None
    for when, line, details in sorted(events, key=lambda e: e[0] or now, reverse=True):
        w = when_phrase(when, now)
        lines = []
        if w != last_when:
            lines.append(f"{w}:")
            last_when = w
        if line:
            lines.append(line)
        lines.extend(details)
        blocks.append("\n".join(lines))
    return f"{friend} Update!\n\n" + "\n\n".join(blocks) + f"\n\n\n\nfull profile: {profile_url}\n"


# ---------------------------------------------------------------- email / state
def send_email(subject, body):
    sender = os.environ["GMAIL_ADDRESS"]
    to = os.environ.get("ALERT_TO") or sender
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(sender, os.environ["GMAIL_APP_PASSWORD"].replace(" ", ""))
        s.send_message(msg)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"failures": 0, "fail_alert_sent": False, "last_saved": None}


def save_state(state):
    if "seen_status" in state:
        state["seen_status"] = state["seen_status"][-300:]
    state["last_saved"] = datetime.now(CENTRAL).isoformat(timespec="seconds")
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status-file")
    ap.add_argument("--profile-file")
    ap.add_argument("--now", help="pretend current time (ISO, for testing)")
    args = ap.parse_args()
    dry_run = args.dry_run or os.environ.get("DRY_RUN", "").lower() == "true"

    friend = os.environ.get("FRIEND_NAME", "").strip()
    profile_url = os.environ.get("PROFILE_URL", "").strip()
    if not friend or not re.match(r"https://www\.goodreads\.com/user/show/\d+", profile_url):
        print("Set FRIEND_NAME and PROFILE_URL (https://www.goodreads.com/user/show/<id>-<name>) "
              "as GitHub secrets, or in a .env file for local runs.", file=sys.stderr)
        return 1
    slug = profile_url.split("?")[0].rstrip("/").rsplit("/", 1)[-1]     # "<id>-<name>"
    status_url = f"https://www.goodreads.com/user_status/list/{slug}"
    now = datetime.fromisoformat(args.now).astimezone(CENTRAL) if args.now else datetime.now(CENTRAL)
    subject = f"Goodreads {friend} Update"

    state = load_state()

    try:
        status_page = Path(args.status_file).read_text(encoding="utf-8") if args.status_file else fetch(status_url)
        profile_page = Path(args.profile_file).read_text(encoding="utf-8") if args.profile_file else fetch(profile_url)
        statuses = parse_statuses(status_page, now)
        reading, shelves = parse_profile(profile_page, now)
        if not statuses and not shelves:
            raise ValueError("found no status updates and no shelves (page layout changed, or profile private?)")
        print(f"Read {len(statuses)} status updates, {len(reading)} currently-reading books, "
              f"{len(shelves)} shelves")
    except Exception as e:
        print(f"Fetch/parse failed: {e}", file=sys.stderr)
        state["failures"] = state.get("failures", 0) + 1
        if state["failures"] >= FAIL_ALERT_AFTER and not state.get("fail_alert_sent"):
            body = (f"The Goodreads check for {friend} has failed {state['failures']} runs in a row.\n"
                    f"Last error: {e}\n")
            if dry_run:
                print(body)
            else:
                try:
                    send_email(f"{subject}: check failing", body)
                except Exception as mail_err:
                    print(f"Could not send failure email: {mail_err}", file=sys.stderr)
            state["fail_alert_sent"] = True
        if not dry_run:
            save_state(state)
        return 0  # don't mark the workflow red for a transient Goodreads hiccup

    first_run = "seen_status" not in state
    if state.get("failures"):
        print(f"Recovered after {state['failures']} failed runs.")

    if first_run and not dry_run:
        print("First run: recording what's there now as already seen; no email.")
        events = []
    elif first_run:
        # Dry run with no saved state: preview the email her newest status would produce.
        print("Dry run with no saved state: previewing her newest status update only.")
        preview_state = {"seen_status": [s["id"] for s in statuses[1:]],
                         "reading": reading, "shelves": shelves}
        events = find_events(preview_state, statuses, reading, shelves, now)
    else:
        events = find_events(state, statuses, reading, shelves, now)

    if events:
        body = build_email(events, friend, profile_url, now)
        if IN_ACTIONS:
            # Public log: say how much, not what.
            print(f"{'Would send' if dry_run else 'Sending'} an email with {len(events)} update(s).")
        else:
            print(body)
        if not dry_run:
            send_email(subject, body)
    else:
        print("Nothing new.")

    new_state = {
        "seen_status": state.get("seen_status", []) +
                       [s["id"] for s in reversed(statuses) if s["id"] not in set(state.get("seen_status", []))],
        "reading": {bid: {"title": b["title"], "author": b["author"]} for bid, b in reading.items()},
        "shelves": shelves,
        "authors": {**state.get("authors", {}),
                    **{bid: b["author"] for bid, b in reading.items() if b["author"]}},
        "failures": 0,
        "fail_alert_sent": False,
        "last_saved": state.get("last_saved"),
    }
    comparable = ("seen_status", "reading", "shelves", "authors", "failures", "fail_alert_sent")
    changed = any(new_state[k] != state.get(k) for k in comparable)
    if changed and not dry_run:
        save_state(new_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
