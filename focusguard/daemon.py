"""FocusGuard daemon: enforces the block and serves the local UI.

Installed mode runs as SYSTEM from a scheduled task. For a safe local trial:
    python focusguard/daemon.py --dry-run [--fast]
"""
import argparse
import copy
import json
import logging
import os
import secrets
import shutil
import tempfile
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import core
from core import GuardError
from enforce import REAL_HOSTS, Enforcer

log = logging.getLogger("focusguard")

DEFAULT_DATA_DIR = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "FocusGuard"
DEFAULT_PORT = 8765
TICK_SECONDS = 5
HEARTBEAT_GRACE = 15  # seconds without a heartbeat before the cooldown resets


class Guard:
    def __init__(self, data_dir, enforcer, seconds_per_minute=60, dry_run=False):
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.enforcer = enforcer
        self.spm = seconds_per_minute
        self.dry_run = dry_run
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.pending_path = self.dir / "pending.json"
        self.state_path = self.dir / "state.json"
        self.history_path = self.dir / "unlocks.jsonl"

        self.config = self._load_config()
        self.pending = self._load_json(self.pending_path, {"config": None, "uninstall_at": None})
        self.state = self._load_json(self.state_path, {"unlocks": {}, "uninstall_approved": False})
        self.history = self._load_history()
        self.request = None  # in-memory only: restarting the daemon abandons a cooldown

    # ------------------------------------------------------------ persistence

    def _load_json(self, path, default):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return {**default, **data} if isinstance(data, dict) else copy.deepcopy(default)
        except (OSError, ValueError):
            return copy.deepcopy(default)

    def _load_config(self):
        raw = self._load_json(self.config_path, {})
        try:
            return core.validate_config(raw)
        except ValueError:
            if raw:
                log.warning("config.json is invalid; falling back to defaults")
            config = core.validate_config(core.DEFAULT_CONFIG)
            self._write_json(self.config_path, config)
            return config

    def _load_history(self):
        entries = []
        try:
            with open(self.history_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        pass
        except OSError:
            pass
        return entries

    def _write_json(self, path, data):
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

    def _append_history(self, entry):
        self.history.append(entry)
        with open(self.history_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    # ------------------------------------------------------------ derived state

    def _unlocks_today(self, now):
        today = core.local_date(now)
        return sum(1 for e in self.history
                   if e.get("outcome") == "granted" and core.local_date(e["ts"]) == today)

    def _active_unlocks(self, now):
        return {s: t for s, t in self.state["unlocks"].items() if t > now}

    def _blocked_sites(self, now):
        if self.state["uninstall_approved"]:
            return []
        if not core.is_block_active(self.config["schedule"], datetime.fromtimestamp(now)):
            return []
        unlocked = self._active_unlocks(now)
        return [s for s in self.config["sites"] if s not in unlocked]

    def _request_view(self, now):
        r = self.request
        if not r:
            return None
        start = now if now - r["last_heartbeat"] > HEARTBEAT_GRACE else r["start"]
        return {
            "id": r["id"], "site": r["site"], "minutes": r["minutes"], "reason": r["reason"],
            "cooldown_s": r["cooldown_s"],
            "remaining_s": max(0, round(r["cooldown_s"] - (now - start))),
            "resets": r["resets"],
        }

    def status(self):
        with self.lock:
            now = time.time()
            dt = datetime.fromtimestamp(now)
            window = core.is_block_active(self.config["schedule"], dt)
            nxt = core.next_change(self.config["schedule"], dt)
            blocked = set(self._blocked_sites(now))
            unlocked = self._active_unlocks(now)
            sites = []
            for s in self.config["sites"]:
                if s in blocked:
                    sites.append({"site": s, "state": "blocked"})
                elif s in unlocked and window:
                    sites.append({"site": s, "state": "unlocked", "until": unlocked[s]})
                else:
                    sites.append({"site": s, "state": "open"})
            today = self._unlocks_today(now)
            return {
                "now": now,
                "window_active": window,
                "next_change": nxt.timestamp() if nxt else None,
                "sites": sites,
                "unlocks_today": today,
                "next_cooldown_s": core.cooldown_seconds(self.config, today, self.spm),
                "config": self.config,
                "pending": self.pending,
                "uninstall_approved": self.state["uninstall_approved"],
                "request": self._request_view(now),
                "dry_run": self.dry_run,
                "seconds_per_minute": self.spm,
            }

    def history_view(self, limit=200):
        with self.lock:
            return list(reversed(self.history[-limit:]))

    # ------------------------------------------------------------ unlock flow

    def _abandon_request(self, why):
        r = self.request
        if r:
            self._append_history({
                "ts": time.time(), "site": r["site"], "minutes": r["minutes"],
                "reason": r["reason"], "cooldown_s": r["cooldown_s"],
                "outcome": "abandoned", "note": why,
            })
            self.request = None

    def start_unlock(self, site, minutes, reason):
        with self.lock:
            now = time.time()
            if site not in self._blocked_sites(now):
                raise GuardError(f"{site} isn't blocked right now.")
            try:
                minutes = int(minutes)
            except (TypeError, ValueError):
                raise GuardError("Pick a duration.")
            if not 1 <= minutes <= self.config["max_unlock_min"]:
                raise GuardError(f"Unlocks can last at most {self.config['max_unlock_min']} minutes.")
            try:
                reason = core.validate_reason(reason, self.config["min_reason_chars"])
            except ValueError as e:
                raise GuardError(str(e))
            self._abandon_request("replaced by a new request")
            self.request = {
                "id": secrets.token_hex(8), "site": site, "minutes": minutes, "reason": reason,
                "cooldown_s": core.cooldown_seconds(self.config, self._unlocks_today(now), self.spm),
                "start": now, "last_heartbeat": now, "resets": 0, "created": now,
            }
            log.info("Unlock requested for %s (%d min): %s", site, minutes, reason)
            return self._request_view(now)

    def _current_request(self, request_id):
        if not self.request or self.request["id"] != request_id:
            raise GuardError("That unlock request is no longer active.", 409)
        return self.request

    def heartbeat(self, request_id):
        with self.lock:
            now = time.time()
            r = self._current_request(request_id)
            if now - r["last_heartbeat"] > HEARTBEAT_GRACE:
                r["start"] = now
                r["resets"] += 1
            r["last_heartbeat"] = now
            return self._request_view(now)

    def complete_unlock(self, request_id):
        with self.lock:
            now = time.time()
            r = self._current_request(request_id)
            if now - r["last_heartbeat"] > HEARTBEAT_GRACE:
                r["start"], r["last_heartbeat"] = now, now
                r["resets"] += 1
                raise GuardError("You left the page, so the timer restarted.", 409)
            remaining = r["cooldown_s"] - (now - r["start"])
            if remaining > 0:
                raise GuardError(f"{round(remaining)} seconds left to wait.", 409)
            if r["site"] not in self._blocked_sites(now):
                self.request = None
                raise GuardError(f"{r['site']} isn't blocked anymore.", 409)
            until = now + r["minutes"] * self.spm
            self.state["unlocks"][r["site"]] = until
            self._write_json(self.state_path, self.state)
            self._append_history({
                "ts": now, "site": r["site"], "minutes": r["minutes"], "reason": r["reason"],
                "cooldown_s": r["cooldown_s"], "waited_s": round(now - r["created"]),
                "resets": r["resets"], "outcome": "granted",
            })
            log.info("Unlocked %s until %s", r["site"], datetime.fromtimestamp(until))
            self.request = None
            self.wake.set()
            return {"site": r["site"], "until": until}

    def cancel_unlock(self, request_id):
        with self.lock:
            self._current_request(request_id)
            self._abandon_request("cancelled")

    def relock(self, site):
        with self.lock:
            if self.state["unlocks"].pop(site, None) is not None:
                self._write_json(self.state_path, self.state)
                log.info("Re-locked %s early", site)
            self.wake.set()

    # ------------------------------------------------------------ settings

    def save_config(self, raw):
        with self.lock:
            try:
                new = core.validate_config(raw)
            except ValueError as e:
                raise GuardError(str(e))
            now = time.time()
            immediate, pending = core.plan_config_change(self.config, new, now)
            self.config = immediate
            self._write_json(self.config_path, immediate)
            self.pending["config"] = pending
            self._write_json(self.pending_path, self.pending)
            log.info("Config saved; pending loosening: %s", bool(pending))
            self.wake.set()
            return {"applied": immediate, "pending": pending}

    def cancel_pending(self, kind):
        with self.lock:
            if kind not in ("config", "uninstall"):
                raise GuardError("Unknown pending item.")
            self.pending["config" if kind == "config" else "uninstall_at"] = None
            self._write_json(self.pending_path, self.pending)

    def request_uninstall(self):
        with self.lock:
            if not self.pending["uninstall_at"]:
                self.pending["uninstall_at"] = time.time() + self.config["loosen_delay_hours"] * 3600
                self._write_json(self.pending_path, self.pending)
                log.info("Uninstall requested")
            return {"uninstall_at": self.pending["uninstall_at"]}

    # ------------------------------------------------------------ main loop

    def tick(self):
        with self.lock:
            now = time.time()
            pending = self.pending
            if pending["config"] and pending["config"]["apply_at"] <= now:
                self.config = core.validate_config(pending["config"]["config"])
                self._write_json(self.config_path, self.config)
                pending["config"] = None
                self._write_json(self.pending_path, pending)
                log.info("Pending config change applied")
            if pending["uninstall_at"] and pending["uninstall_at"] <= now \
                    and not self.state["uninstall_approved"]:
                self.state["uninstall_approved"] = True
                self._write_json(self.state_path, self.state)
                log.info("Uninstall approved: enforcement disabled")

            expired = [s for s, t in self.state["unlocks"].items() if t <= now]
            for s in expired:
                del self.state["unlocks"][s]
                log.info("Unlock for %s expired", s)
            if expired:
                self._write_json(self.state_path, self.state)

            if self.request and now - self.request["created"] > 6 * 3600:
                self._abandon_request("expired")

            self.enforcer.apply(self._blocked_sites(now))


def setup_logging(data_dir, console):
    handlers = [RotatingFileHandler(Path(data_dir) / "daemon.log", maxBytes=1_000_000,
                                    backupCount=2, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")


def main():
    ap = argparse.ArgumentParser(description="FocusGuard site blocker daemon")
    ap.add_argument("--dry-run", action="store_true",
                    help="use a scratch data dir and fake hosts file; don't touch the registry")
    ap.add_argument("--fast", action="store_true",
                    help="(dry run only) treat every 'minute' as one second for testing")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--data-dir")
    args = ap.parse_args()
    if args.fast and not args.dry_run:
        ap.error("--fast is only allowed with --dry-run")

    from server import make_server
    try:
        server = make_server(args.port)
    except OSError:
        # The port doubles as a single-instance lock: the scheduled task
        # relaunches us every minute and those extra copies exit here.
        return 0

    if args.dry_run:
        data_dir = Path(args.data_dir or Path(tempfile.gettempdir()) / "focusguard-dryrun")
        data_dir.mkdir(parents=True, exist_ok=True)
        hosts = data_dir / "hosts"
        if not hosts.exists():
            shutil.copyfile(REAL_HOSTS, hosts)
        enforcer = Enforcer(str(hosts), use_registry=False, flush_dns=False)
    else:
        data_dir = Path(args.data_dir or DEFAULT_DATA_DIR)
        data_dir.mkdir(parents=True, exist_ok=True)
        enforcer = Enforcer()

    setup_logging(data_dir, console=args.dry_run)
    server.guard = Guard(data_dir, enforcer, seconds_per_minute=1 if args.fast else 60,
                         dry_run=args.dry_run)
    guard = server.guard
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("FocusGuard running on http://127.0.0.1:%d (data: %s)", args.port, data_dir)

    while True:
        try:
            guard.tick()
        except Exception:
            log.exception("Tick failed")
        guard.wake.wait(TICK_SECONDS)
        guard.wake.clear()


if __name__ == "__main__":
    raise SystemExit(main())
