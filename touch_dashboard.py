#!/usr/bin/env python3
"""Read-only GitHub CI dashboard for RGB565 framebuffer + ADS7846 touch."""

import argparse
from datetime import datetime, timezone
import math
import json
import subprocess
from pathlib import Path
from queue import Empty, Queue
import select
import statistics
import sys
import threading
import time

from evdev import InputDevice, list_devices, ecodes
from PIL import Image, ImageDraw, ImageFont

# GitHub data collection. Uses the authenticated GitHub CLI, never a local clone.

class CollectionError(RuntimeError):
    """GitHub API response could not be collected or decoded."""


def gh_api(endpoint, **params):
    command = ["gh", "api", "--method", "GET", endpoint]
    for name, value in params.items():
        command += ["-f", f"{name}={value}"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CollectionError(f"Cannot call GitHub API ({endpoint}): {exc}") from exc
    if result.returncode:
        raise CollectionError(f"GitHub API failed ({endpoint}): {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CollectionError(f"Invalid JSON from {endpoint}") from exc


def list_all(endpoint, field, **params):
    items = []
    page = 1
    while True:
        response = gh_api(endpoint, per_page=100, page=page, **params)
        batch = response[field]
        total = response["total_count"]
        items.extend(batch)
        if len(items) >= total:
            return items
        if not batch:
            raise CollectionError(f"Pagination stopped early: {endpoint}")
        page += 1


def search_open_prs(login):
    items = []
    page = 1
    while True:
        response = gh_api(
            "search/issues", q=f"is:pr is:open author:{login}",
            sort="updated", order="desc", per_page=100, page=page,
        )
        if response["incomplete_results"]:
            raise CollectionError("GitHub search returned incomplete results")
        if response["total_count"] > 1000:
            raise CollectionError("GitHub search exceeds its 1,000-result limit")
        batch = response["items"]
        items.extend(batch)
        if len(items) >= response["total_count"]:
            return items
        if not batch:
            raise CollectionError("PR search pagination stopped early")
        page += 1


def read_commit_checks(repo, sha):
    base = f"repos/{repo}/commits/{sha}"
    checks = list_all(base + "/check-runs", "check_runs", filter="latest")
    statuses = list_all(base + "/status", "statuses")
    latest = {}
    for item in statuses:
        context = item["context"]
        if context not in latest or item.get("updated_at", "") > latest[context].get("updated_at", ""):
            latest[context] = item
    return checks, list(latest.values())


def classify(checks, statuses):
    values = [
        run.get("conclusion") if run.get("status") == "completed"
        else run.get("status")
        for run in checks
    ]
    values += [item.get("state") for item in statuses]
    if not values:
        return "NO CHECKS"
    if any(v in ("failure", "error", "timed_out", "startup_failure") for v in values):
        return "FAILED"
    if "action_required" in values:
        return "ACTION REQUIRED"
    if "cancelled" in values:
        return "CANCELLED"
    if "in_progress" in values:
        return "RUNNING"
    if any(v in ("queued", "waiting", "requested", "pending") for v in values):
        return "QUEUED / PENDING"
    if "stale" in values:
        return "STALE CHECK"
    if "neutral" in values:
        return "NEUTRAL"
    if any(v not in ("success", "skipped") for v in values):
        return "UNKNOWN"
    if all(v == "skipped" for v in values):
        return "SKIPPED"
    if "skipped" in values:
        return "PASSED + SKIPS"
    return "PASSED"


def collect():
    login = gh_api("user")["login"]
    found = search_open_prs(login)
    output = []
    for item in found:
        repo = item["repository_url"].split("/repos/", 1)[1]
        number = item["number"]
        pr = gh_api(f"repos/{repo}/pulls/{number}")
        head_sha = pr["head"]["sha"]
        checks, statuses = read_commit_checks(repo, head_sha)
        source = "HEAD"
        merge_sha = pr.get("merge_commit_sha")
        if not checks and not statuses and merge_sha and merge_sha != head_sha:
            merge_checks, merge_statuses = read_commit_checks(repo, merge_sha)
            if merge_checks or merge_statuses:
                checks, statuses = merge_checks, merge_statuses
                source = "MERGE"
        completed = sum(run.get("status") == "completed" for run in checks)
        output.append({
            "repo": repo,
            "number": number,
            "title": pr["title"],
            "draft": pr["draft"],
            "sha": head_sha,
            "checks_source": source,
            "state": classify(checks, statuses),
            "completed": completed,
            "total": len(checks),
            "checks": [{
                "name": run["name"],
                "status": run["status"],
                "conclusion": run.get("conclusion"),
                "app": (run.get("app") or {}).get("slug"),
            } for run in checks],
            "commit_statuses": [{
                "context": status["context"],
                "state": status["state"],
            } for status in statuses],
        })
    return {
        "account": login,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total": len(output),
        "prs": output,
    }

# Native 480 x 320 / 16-bpp RGB565 framebuffer drawing.

FB = "/dev/fb0"
WIDTH, HEIGHT = 480, 320
PAGE_SIZE = 5
PAGE_SECONDS = 8

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
fonts = {
    "header": ImageFont.truetype(str(FONT_DIR / "DejaVuSans-Bold.ttf"), 19),
    "body": ImageFont.truetype(str(FONT_DIR / "DejaVuSans.ttf"), 13),
    "bold": ImageFont.truetype(str(FONT_DIR / "DejaVuSans-Bold.ttf"), 12),
    "small": ImageFont.truetype(str(FONT_DIR / "DejaVuSans.ttf"), 11),
}
COLORS = {
    "NO CHECKS": "#8994a5", "PASSED": "#35c878",
    "PASSED + SKIPS": "#e2b65b", "FAILED": "#f06464",
    "ACTION REQUIRED": "#f06464", "CANCELLED": "#e89d60",
    "RUNNING": "#46a7ff", "QUEUED / PENDING": "#e2b65b",
    "SKIPPED": "#e2b65b", "NEUTRAL": "#8994a5",
    "STALE CHECK": "#e89d60", "UNKNOWN": "#8994a5",
}
LABELS = {
    "PASSED + SKIPS": "PASS + SKIP",
    "QUEUED / PENDING": "PENDING",
    "ACTION REQUIRED": "ACTION",
    "STALE CHECK": "STALE",
}


def shorten(draw, value, font, width):
    text = str(value)
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "...", font=font) > width:
        text = text[:-1]
    return text + "..."


def draw_page(snapshot, page):
    image = Image.new("RGB", (WIDTH, HEIGHT), "#10151f")
    draw = ImageDraw.Draw(image)
    prs = snapshot["prs"]
    visible = prs[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    pages = max(1, math.ceil(len(prs) / PAGE_SIZE))

    draw.rectangle((0, 0, WIDTH, 44), fill="#192230")
    draw.text((13, 5), "GitHub CI", font=fonts["header"], fill="#f0f4fa")
    draw.text((14, 28), f"{snapshot['account']}  ·  {len(prs)} open PRs",
              font=fonts["small"], fill="#aab5c4")
    for index, pr in enumerate(visible):
        y = 46 + index * 47
        if index % 2:
            draw.rectangle((0, y, WIDTH - 1, y + 46), fill="#151d29")
        color = COLORS.get(pr["state"], COLORS["UNKNOWN"])
        label = LABELS.get(pr["state"], pr["state"])
        identity = shorten(draw, f"{pr['repo']} #{pr['number']}", fonts["bold"], 245)
        draw.text((13, y + 4), identity, font=fonts["bold"], fill="#f0f4fa")
        if pr["draft"]:
            offset = draw.textlength(identity, font=fonts["bold"])
            draw.text((min(21 + offset, 300), y + 5), "DRAFT",
                      font=fonts["small"], fill="#ab91e9")
        label_width = draw.textlength(label, font=fonts["bold"])
        draw.text((466 - label_width, y + 5), label,
                  font=fonts["bold"], fill=color)
        title = shorten(draw, pr["title"], fonts["body"], 305)
        draw.text((13, y + 23), title, font=fonts["body"], fill="#aab5c4")
        x, bar_y, bar_w = 338, y + 32, 86
        draw.rounded_rectangle((x, bar_y, x + bar_w, bar_y + 6),
                               radius=3, fill="#303c4c")
        if pr["total"] > 0:
            filled = round(bar_w * min(1, pr["completed"] / pr["total"]))
            if filled:
                draw.rounded_rectangle((x, bar_y, x + filled, bar_y + 6),
                                       radius=3, fill=color)
        count = f"{pr['completed']}/{pr['total']}"
        count_width = draw.textlength(count, font=fonts["small"])
        draw.text((466 - count_width, y + 25), count,
                  font=fonts["small"], fill="#d2dae6")
        draw.line((12, y + 46, 468, y + 46), fill="#293342")
    if not prs:
        draw.text((32, 135), "No open pull requests",
                  font=fonts["header"], fill="#aab5c4")
    draw.rectangle((0, 283, WIDTH - 1, HEIGHT - 1), fill="#192230")
    stamp = datetime.fromisoformat(snapshot["retrieved_at"]).strftime("%H:%M:%S")
    draw.text((13, 292), f"Fetched {stamp} UTC", font=fonts["small"], fill="#aab5c4")
    draw.text((424, 292), f"{page + 1}/{pages}", font=fonts["bold"], fill="#f0f4fa")
    return image


def to_rgb565(image):
    raw = bytearray(WIDTH * HEIGHT * 2)
    for i, (r, g, b) in enumerate(image.getdata()):
        value = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
        raw[2*i] = value & 0xff
        raw[2*i + 1] = value >> 8
    return raw


def verify_framebuffer():
    root = Path("/sys/class/graphics/fb0")
    size = tuple(map(int, (root / "virtual_size").read_text().strip().split(",")))
    bpp = int((root / "bits_per_pixel").read_text())
    stride = int((root / "stride").read_text())
    if (size, bpp, stride) != ((WIDTH, HEIGHT), 16, WIDTH*2):
        raise RuntimeError(f"Unexpected framebuffer: {size}, {bpp}bpp, stride {stride}")
    if sys.byteorder != "little":
        raise RuntimeError("Expected little-endian RGB565")


HEADER = "#192230"
BG = "#10151f"
ALT = "#151d29"
FG = "#f0f4fa"
MUTED = "#aab5c4"
GREEN = "#35c878"
AMBER = "#e2b65b"
RED = "#f06464"

# Approximate mapping from the three consistent targets already measured:
# screen (48,55) -> raw (702,3286)
# screen (432,55) -> raw (3703,3223)
# screen (432,265) -> raw (3639,958)
# Edit these only if touch targets prove misaligned.
RAW_LEFT, RAW_RIGHT = 702, 3703
RAW_TOP, RAW_BOTTOM = 3255, 958


def touch_xy(raw_x, raw_y):
    x = 48 + (raw_x - RAW_LEFT) * 384 / (RAW_RIGHT - RAW_LEFT)
    y = 55 + (raw_y - RAW_TOP) * 210 / (RAW_BOTTOM - RAW_TOP)
    return (max(0, min(WIDTH - 1, round(x))),
            max(0, min(HEIGHT - 1, round(y))))


def touch_device():
    for path in list_devices():
        device = InputDevice(path)
        axes = device.capabilities(absinfo=False).get(ecodes.EV_ABS, [])
        keys = device.capabilities(absinfo=False).get(ecodes.EV_KEY, [])
        if ("ADS7846" in device.name and ecodes.ABS_X in axes
                and ecodes.ABS_Y in axes and ecodes.BTN_TOUCH in keys):
            return device
        device.close()
    raise RuntimeError("ADS7846 touch input device not available")


class TouchReader:
    def __init__(self, device):
        self.device = device
        self.pressed = False
        self.x = self.y = None
        self.samples = []
        self.started = 0

    def poll(self):
        """Return screen tap (x,y) on release, or None."""
        taps = []
        while select.select([self.device], [], [], 0)[0]:
            try:
                events = self.device.read()
            except BlockingIOError:
                break
            for event in events:
                if event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
                    if event.value == 1:
                        self.pressed = True
                        self.started = time.monotonic()
                        self.samples = []
                        self.x = self.y = None
                    elif event.value == 0 and self.pressed:
                        self.pressed = False
                        if self.samples and time.monotonic() - self.started < 10:
                            xs = [p[0] for p in self.samples]
                            ys = [p[1] for p in self.samples]
                            taps.append(touch_xy(statistics.median(xs), statistics.median(ys)))
                elif event.type == ecodes.EV_ABS:
                    if event.code == ecodes.ABS_X:
                        self.x = event.value
                    elif event.code == ecodes.ABS_Y:
                        self.y = event.value
                elif (event.type == ecodes.EV_SYN and event.code == ecodes.SYN_REPORT
                      and self.pressed and self.x is not None and self.y is not None):
                    self.samples.append((self.x, self.y))
                    if len(self.samples) > 30:
                        self.samples.pop(0)
        return taps[-1] if taps else None


def button(draw, box, label, enabled=True):
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=5, fill="#303c4c" if enabled else "#222b38")
    color = FG if enabled else "#6e7a89"
    w = draw.textlength(label, font=fonts["bold"])
    draw.text((x1 + (x2-x1-w)/2, y1+7), label, font=fonts["bold"], fill=color)


def indicator(draw, state):
    text, color = state
    draw.rectangle((360, 0, 479, 43), fill=HEADER)
    width = draw.textlength(text, font=fonts["bold"])
    draw.text((466-width, 14), text, font=fonts["bold"], fill=color)


def detail_page(snapshot, selected, index):
    pr = next((p for p in snapshot["prs"]
               if (p["repo"], p["number"]) == selected), None)
    if pr is None:
        return None, 1
    items = [("CHECK", c["name"], c["conclusion"] if c["status"] == "completed" else c["status"])
             for c in pr["checks"]]
    items += [("STATUS", s["context"], s["state"]) for s in pr["commit_statuses"]]
    pages = max(1, math.ceil(len(items) / 5))
    index = min(index, pages - 1)

    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH, 43), fill=HEADER)
    draw.text((13, 5), f"PR #{pr['number']}", font=fonts["header"], fill=FG)
    draw.text((14, 29), shorten(draw, pr["repo"], fonts["small"], 275),
              font=fonts["small"], fill=MUTED)

    title = shorten(draw, pr["title"], fonts["body"], 454)
    draw.text((13, 49), title, font=fonts["body"], fill=FG)
    summary = f"{pr['completed']}/{pr['total']} checks  ·  {len(pr['commit_statuses'])} legacy status"
    if pr["draft"]:
        summary += "  ·  DRAFT"
    draw.text((13, 73), shorten(draw, summary, fonts["small"], 450),
              font=fonts["small"], fill=MUTED)
    draw.line((12, 92, 468, 92), fill="#293342")

    shown = items[index * 5:(index+1) * 5]
    if not items:
        draw.text((23, 142), "No checks found for this PR", font=fonts["body"], fill=MUTED)
    for i, (kind, name, state) in enumerate(shown):
        y = 99 + i * 35
        if i % 2:
            draw.rectangle((0, y, 479, y+34), fill=ALT)
        color = {
            "success": GREEN, "skipped": AMBER, "failure": RED,
            "error": RED, "timed_out": RED, "cancelled": "#e89d60",
            "in_progress": "#46a7ff", "queued": AMBER,
            "waiting": AMBER, "pending": AMBER, "requested": AMBER,
        }.get(state, MUTED)
        state_label = str(state or "unknown").upper().replace("IN_PROGRESS", "RUNNING")
        sw = draw.textlength(state_label, font=fonts["small"])
        name_text = ("S: " if kind == "STATUS" else "") + name
        draw.text((13, y+10), shorten(draw, name_text, fonts["body"], max(90, 443-sw)),
                  font=fonts["body"], fill=FG)
        draw.text((466-sw, y+11), state_label, font=fonts["small"], fill=color)
    return image, pages


def worker_fetch(queue):
    try:
        queue.put((True, collect()))
    except Exception as exc:
        queue.put((False, str(exc)))


def run(interval):
    verify_framebuffer()
    device = touch_device()
    touch = TouchReader(device)
    queue = Queue(maxsize=1)
    snapshot = None
    last_success = None
    error = None
    fetching = False
    failures = 0
    next_fetch = 0.0
    last_manual = -100.0
    selected = None
    detail_index = 0
    list_base = 0
    list_epoch = time.monotonic()
    version = 0
    last_key = None
    size = WIDTH*HEIGHT*2

    try:
        with open(FB, "r+b", buffering=0) as fb:
            original = fb.read(size)
            if len(original) != size:
                raise RuntimeError("Could not read complete framebuffer")
            try:
                print(f"Touch dashboard started. Refresh: {interval//60}m. Ctrl+C to stop.", flush=True)
                while True:
                    now = time.monotonic()
                    try:
                        ok, result = queue.get_nowait()
                    except Empty:
                        pass
                    else:
                        fetching = False
                        version += 1
                        if ok:
                            snapshot = result
                            last_success = now
                            failures = 0
                            error = None
                            if selected and not any(
                                (p["repo"], p["number"]) == selected for p in snapshot["prs"]
                            ):
                                selected = None
                            print(f"Refresh OK: {snapshot['total']} PRs ({snapshot['retrieved_at']})", flush=True)
                        else:
                            failures += 1
                            error = result
                            retry = min(interval, 60 * 2**min(failures-1, 4))
                            next_fetch = now + retry
                            print(f"Refresh failed: {error}; retry in {retry}s", file=sys.stderr, flush=True)

                    if not fetching and now >= next_fetch:
                        fetching = True
                        next_fetch = now + interval
                        threading.Thread(target=worker_fetch, args=(queue,), daemon=True).start()
                        version += 1
                        print("Fetching GitHub data...", flush=True)

                    pages = max(1, math.ceil(len(snapshot["prs"])/PAGE_SIZE)) if snapshot else 1
                    list_page = (list_base + int((now-list_epoch)//PAGE_SECONDS)) % pages
                    age = int(now-last_success) if last_success is not None else 0
                    stale = bool(error) or (last_success is not None and age >= interval+120)
                    if snapshot is None:
                        state = ("ERROR", RED) if error else ("LOADING", AMBER)
                    elif stale:
                        state = ("STALE", RED)
                    elif fetching:
                        state = ("SYNCING", AMBER)
                    else:
                        state = ("SYNCED", GREEN)

                    tap = touch.poll()
                    if tap is not None:
                        x, y = tap
                        print(f"Touch: ({x},{y})", flush=True)
                        if y >= 283:
                            if x >= 341:
                                if not fetching and now-last_manual >= 10:
                                    last_manual = now
                                    next_fetch = now
                                    print("Manual refresh requested", flush=True)
                            elif selected is not None:
                                if x < 83:
                                    selected = None
                                    list_epoch = now
                                elif x < 165:
                                    detail_index = max(0, detail_index-1)
                                elif x < 255:
                                    detail_index += 1
                            elif pages > 1:
                                if x < 83:
                                    list_base = (list_page-1) % pages
                                    list_epoch = now
                                elif x < 165:
                                    list_base = (list_page+1) % pages
                                    list_epoch = now
                        elif selected is None and snapshot and 46 <= y < 281:
                            i = list_page*PAGE_SIZE + (y-46)//47
                            if i < len(snapshot["prs"]):
                                pr = snapshot["prs"][i]
                                selected = (pr["repo"], pr["number"])
                                detail_index = 0
                        version += 1

                    key = (selected, detail_index, list_page, version,
                           age//60, stale, fetching, state[0])
                    if key != last_key:
                        if selected is None:
                            if snapshot:
                                image = draw_page(snapshot, list_page)
                            else:
                                placeholder = {"account": "GitHub", "prs": [],
                                               "retrieved_at": datetime.now(timezone.utc).isoformat()}
                                image = draw_page(placeholder, 0)
                                d = ImageDraw.Draw(image)
                                d.rectangle((0, 90, 479, 250), fill=BG)
                                d.text((25, 137), "Connecting to GitHub..." if not error else "GitHub unavailable",
                                       font=fonts["body"], fill=FG)
                        else:
                            image, detail_pages = detail_page(snapshot, selected, detail_index)
                            if image is None:
                                selected = None
                                continue
                            detail_index = min(detail_index, detail_pages-1)

                        draw = ImageDraw.Draw(image)
                        indicator(draw, state)
                        draw.rectangle((0, 283, 479, 319), fill=HEADER)
                        if selected is None:
                            if pages > 1:
                                button(draw, (8, 286, 80, 317), "Prev")
                                button(draw, (88, 286, 160, 317), "Next")
                            footer = f"{list_page+1}/{pages}  ·  {age//60}m old"
                        else:
                            button(draw, (8, 286, 80, 317), "Back")
                            button(draw, (88, 286, 160, 317), "Prev", detail_index>0)
                            button(draw, (168, 286, 248, 317), "Next", detail_index+1<detail_pages)
                            footer = f"{detail_index+1}/{detail_pages}"
                        draw.text((258 if selected else 179, 296), footer,
                                  font=fonts["small"], fill=MUTED)
                        button(draw, (350, 286, 472, 317), "Refresh", not fetching)
                        fb.seek(0)
                        fb.write(to_rgb565(image))
                        last_key = key
                    time.sleep(.08)
            except KeyboardInterrupt:
                print("Stopped.", flush=True)
            finally:
                fb.seek(0)
                fb.write(original)
                print("Display restored.", flush=True)
    finally:
        device.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=int, default=480, help="Polling interval in seconds")
    args = parser.parse_args()
    if args.interval < 60:
        parser.error("--interval must be at least 60 seconds")
    run(args.interval)
