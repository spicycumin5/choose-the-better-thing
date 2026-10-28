"""Pure logic for FocusGuard: schedules, cooldowns, hosts rendering, config changes.

Nothing in here touches the system, so it can be unit tested directly.
"""
import copy
import re
from datetime import datetime, timedelta

MINUTES_PER_DAY = 1440
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY

HOSTS_BEGIN = "# FOCUSGUARD BEGIN - managed automatically, manual edits are overwritten"
HOSTS_END = "# FOCUSGUARD END"
_BEGIN_PREFIX = "# FOCUSGUARD BEGIN"

# A hosts file has no wildcards, so common subdomains are spelled out.
# Browser policies (URLBlocklist) cover every subdomain on their own.
HOST_PREFIXES = ("", "www.", "m.")
HOST_EXTRAS = {
    "youtube.com": ("music.youtube.com", "youtubei.googleapis.com"),
    "instagram.com": ("i.instagram.com", "l.instagram.com", "graph.instagram.com"),
}

DEFAULT_CONFIG = {
    "sites": ["instagram.com", "youtu.be", "youtube.com"],
    "schedule": [
        {"days": [0, 1, 2, 3, 4], "start": "09:00", "end": "22:00"},
        {"days": [5, 6], "start": "10:00", "end": "20:00"},
    ],
    "cooldown_base_min": 5,
    "cooldown_max_min": 60,
    "max_unlock_min": 15,
    "min_reason_chars": 40,
    "loosen_delay_hours": 24,
}

# name -> (min, max, which direction is stricter)
NUMERIC_FIELDS = {
    "cooldown_base_min": (1, 120, "higher"),
    "cooldown_max_min": (1, 480, "higher"),
    "max_unlock_min": (1, 60, "lower"),
    "min_reason_chars": (10, 500, "higher"),
    "loosen_delay_hours": (1, 168, "higher"),
}


class GuardError(Exception):
    """A user-facing error, reported by the API with the given HTTP status."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


_DOMAIN_RE =re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# ---------------------------------------------------------------- sites

def normalize_site(raw):
    s = str(raw).strip().lower()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    s = s.split("/")[0].split("?")[0].split("#")[0].split(":")[0]
    for prefix in ("*.", "www."):
        if s.startswith(prefix):
            s = s[len(prefix):]
    if not _DOMAIN_RE.match(s):
        raise ValueError(f"'{raw}' is not a valid domain")
    return s


def expand_hosts(sites):
    names = set()
    for site in sites:
        names.update(prefix + site for prefix in HOST_PREFIXES)
        names.update(HOST_EXTRAS.get(site, ()))
    return sorted(names)


def render_hosts(text, domains):
    """Return hosts text with our managed block replaced (or removed if no domains)."""
    out, inside = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_BEGIN_PREFIX):
            inside = True
            continue
        if inside:
            if stripped.startswith(HOSTS_END):
                inside = False
            continue
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    if domains:
        if out:
            out.append("")
        out.append(HOSTS_BEGIN)
        out.extend(f"0.0.0.0 {d}" for d in domains)
        out.extend(f":: {d}" for d in domains)
        out.append(HOSTS_END)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- schedule

def parse_hhmm(value):
    m = _HHMM_RE.match(str(value))
    if not m:
        raise ValueError(f"'{value}' is not a valid HH:MM time")
    return int(m.group(1)) * 60 + int(m.group(2))


def window_minutes(window):
    """Minute-of-week indexes (Monday 00:00 = 0) covered by one window.

    A window whose end is <= its start runs past midnight into the next day;
    start == end means a full 24 hours.
    """
    start, end = parse_hhmm(window["start"]), parse_hhmm(window["end"])
    length = end - start if end > start else end - start + MINUTES_PER_DAY
    covered = set()
    for day in window["days"]:
        base = day * MINUTES_PER_DAY + start
        covered.update((base + i) % MINUTES_PER_WEEK for i in range(length))
    return covered


def schedule_minutes(schedule):
    covered = set()
    for window in schedule:
        covered |= window_minutes(window)
    return frozenset(covered)


def minute_of_week(dt):
    return dt.weekday() * MINUTES_PER_DAY + dt.hour * 60 + dt.minute


def is_block_active(schedule, now):
    return minute_of_week(now) in schedule_minutes(schedule)


def next_change(schedule, now):
    """When the schedule next flips between blocked/open, or None if it never does."""
    covered = schedule_minutes(schedule)
    mow = minute_of_week(now)
    current = mow in covered
    for i in range(1, MINUTES_PER_WEEK + 1):
        if (((mow + i) % MINUTES_PER_WEEK) in covered) != current:
            return now.replace(second=0, microsecond=0) + timedelta(minutes=i)
    return None


# ---------------------------------------------------------------- unlocks

def cooldown_seconds(config, unlocks_today, seconds_per_minute=60):
    minutes = config["cooldown_base_min"] * 2 ** min(unlocks_today, 20)
    return min(minutes, config["cooldown_max_min"]) * seconds_per_minute


def validate_reason(reason, min_chars):
    cleaned = " ".join(str(reason).split())
    if len(cleaned) < min_chars:
        raise ValueError(f"Your reason needs at least {min_chars} characters ({len(cleaned)} so far).")
    words = {w.lower() for w in re.findall(r"[^\W\d_]{2,}", cleaned)}
    if len(words) < 5:
        raise ValueError("Write an actual sentence: at least 5 different words.")
    return cleaned


# ---------------------------------------------------------------- config

def validate_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("Config must be an object")
    sites = sorted({normalize_site(s) for s in raw.get("sites", [])})
    if not sites:
        raise ValueError("Add at least one site")

    schedule = []
    for w in raw.get("schedule", []):
        days = sorted({int(d) for d in w.get("days", [])})
        if not days or any(d < 0 or d > 6 for d in days):
            raise ValueError("Each schedule window needs at least one day (0=Mon .. 6=Sun)")
        parse_hhmm(w.get("start"))
        parse_hhmm(w.get("end"))
        entry = {"days": days, "start": w["start"], "end": w["end"]}
        if entry not in schedule:
            schedule.append(entry)

    config = {"sites": sites, "schedule": schedule}
    for name, (lo, hi, _) in NUMERIC_FIELDS.items():
        try:
            value = int(raw.get(name, DEFAULT_CONFIG[name]))
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a whole number")
        if not lo <= value <= hi:
            raise ValueError(f"{name} must be between {lo} and {hi}")
        config[name] = value
    if config["cooldown_max_min"] < config["cooldown_base_min"]:
        raise ValueError("The cooldown cap can't be lower than the base cooldown")
    return config


def _stricter(name, a, b):
    return max(a, b) if NUMERIC_FIELDS[name][2] == "higher" else min(a, b)


def is_loosening(old, new):
    if set(old["sites"]) - set(new["sites"]):
        return True
    if schedule_minutes(old["schedule"]) - schedule_minutes(new["schedule"]):
        return True
    return any(_stricter(n, old[n], new[n]) != new[n] for n in NUMERIC_FIELDS)


def tighten_merge(old, new):
    """The strictest combination of two configs: what can safely apply right now."""
    merged = {"sites": sorted(set(old["sites"]) | set(new["sites"]))}
    if schedule_minutes(new["schedule"]) >= schedule_minutes(old["schedule"]):
        merged["schedule"] = copy.deepcopy(new["schedule"])
    else:
        merged["schedule"] = copy.deepcopy(old["schedule"])
        merged["schedule"] += [w for w in new["schedule"] if w not in old["schedule"]]
    for name in NUMERIC_FIELDS:
        merged[name] = _stricter(name, old[name], new[name])
    return merged


def plan_config_change(old, new, now_ts):
    """Split a requested change into (apply_now, pending_or_None).

    Tightening applies immediately. Anything that loosens is held back for
    the *current* config's loosen_delay_hours.
    """
    if not is_loosening(old, new):
        return new, None
    immediate = tighten_merge(old, new)
    return immediate, {"config": new, "apply_at": now_ts + old["loosen_delay_hours"] * 3600}


def local_date(ts):
    return datetime.fromtimestamp(ts).date().isoformat()
