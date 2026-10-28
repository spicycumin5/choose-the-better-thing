import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "focusguard"))

import core  # noqa: E402

# 2026-10-26 is a Monday.
MON = datetime(2026, 10, 26)


def at(day_offset, hh, mm=0):
    return MON + timedelta(days=day_offset, hours=hh, minutes=mm)


def cfg(**overrides):
    return core.validate_config({**core.DEFAULT_CONFIG, **overrides})


class ScheduleTests(unittest.TestCase):
    def test_default_weekday_and_weekend(self):
        s = core.DEFAULT_CONFIG["schedule"]
        self.assertFalse(core.is_block_active(s, at(0, 8, 59)))
        self.assertTrue(core.is_block_active(s, at(0, 9, 0)))
        self.assertTrue(core.is_block_active(s, at(0, 21, 59)))
        self.assertFalse(core.is_block_active(s, at(0, 22, 0)))
        self.assertFalse(core.is_block_active(s, at(5, 9, 30)))   # Saturday
        self.assertTrue(core.is_block_active(s, at(5, 10, 0)))

    def test_window_past_midnight(self):
        s = [{"days": [6], "start": "22:00", "end": "02:00"}]     # Sunday night
        self.assertTrue(core.is_block_active(s, at(6, 23, 0)))
        self.assertTrue(core.is_block_active(s, at(0, 1, 59)))    # wraps to Monday
        self.assertFalse(core.is_block_active(s, at(0, 2, 0)))

    def test_equal_start_end_is_full_day(self):
        s = [{"days": [2], "start": "00:00", "end": "00:00"}]
        self.assertTrue(core.is_block_active(s, at(2, 0, 0)))
        self.assertTrue(core.is_block_active(s, at(2, 23, 59)))
        self.assertFalse(core.is_block_active(s, at(3, 0, 0)))

    def test_next_change(self):
        s = core.DEFAULT_CONFIG["schedule"]
        self.assertEqual(core.next_change(s, at(0, 12, 30)), at(0, 22, 0))
        self.assertEqual(core.next_change(s, at(4, 23, 0)), at(5, 10, 0))
        always = [{"days": list(range(7)), "start": "00:00", "end": "00:00"}]
        self.assertIsNone(core.next_change(always, at(0, 1)))


class CooldownTests(unittest.TestCase):
    def test_escalates_and_caps(self):
        c = cfg()
        self.assertEqual([core.cooldown_seconds(c, n) // 60 for n in range(6)], [5, 10, 20, 40, 60, 60])
        self.assertEqual(core.cooldown_seconds(c, 1000), 3600)

    def test_reason_validation(self):
        with self.assertRaises(ValueError):
            core.validate_reason("too short", 40)
        with self.assertRaises(ValueError):
            core.validate_reason("aaaa " * 20, 40)
        ok = core.validate_reason("  I need   the lecture recording my professor posted for class ", 40)
        self.assertNotIn("  ", ok)


class HostsTests(unittest.TestCase):
    ORIGINAL = "# comment\n127.0.0.1 localhost\n10.0.0.235 host.docker.internal\n"

    def test_adds_block_and_keeps_existing_lines(self):
        out = core.render_hosts(self.ORIGINAL, ["youtube.com", "www.youtube.com"])
        self.assertTrue(out.startswith(self.ORIGINAL))
        self.assertIn("0.0.0.0 youtube.com", out)
        self.assertIn(":: www.youtube.com", out)

    def test_idempotent_and_removable(self):
        once = core.render_hosts(self.ORIGINAL, ["youtube.com"])
        self.assertEqual(core.render_hosts(once, ["youtube.com"]), once)
        self.assertEqual(core.render_hosts(once, []), self.ORIGINAL)

    def test_replaces_tampered_block(self):
        once = core.render_hosts(self.ORIGINAL, ["youtube.com"])
        tampered = once.replace("0.0.0.0 youtube.com\n", "")
        self.assertEqual(core.render_hosts(tampered, ["youtube.com"]), once)

    def test_expand_hosts(self):
        names = core.expand_hosts(["youtube.com"])
        for n in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"):
            self.assertIn(n, names)


class ConfigTests(unittest.TestCase):
    def test_normalize_site(self):
        self.assertEqual(core.normalize_site("https://www.YouTube.com/watch?v=x"), "youtube.com")
        self.assertEqual(core.normalize_site("*.reddit.com"), "reddit.com")
        with self.assertRaises(ValueError):
            core.normalize_site("not a site")

    def test_tightening_applies_immediately(self):
        old = cfg()
        new = cfg(sites=old["sites"] + ["reddit.com"], cooldown_base_min=10)
        now_cfg, pending = core.plan_config_change(old, new, 1000)
        self.assertEqual(now_cfg, new)
        self.assertIsNone(pending)

    def test_widening_schedule_is_not_loosening(self):
        old = cfg()
        new = cfg(schedule=[{"days": list(range(7)), "start": "06:00", "end": "23:00"}])
        self.assertFalse(core.is_loosening(old, new))

    def test_loosening_is_delayed_but_tight_parts_apply(self):
        old = cfg()
        new = cfg(sites=["youtube.com", "reddit.com"], cooldown_base_min=1)
        now_cfg, pending = core.plan_config_change(old, new, 1000)
        self.assertEqual(pending["apply_at"], 1000 + 24 * 3600)
        self.assertEqual(pending["config"], new)
        self.assertIn("instagram.com", now_cfg["sites"])     # removal held back
        self.assertIn("reddit.com", now_cfg["sites"])        # addition applied now
        self.assertEqual(now_cfg["cooldown_base_min"], 5)    # lower wait held back

    def test_shrinking_schedule_keeps_old_coverage_now(self):
        old = cfg()
        new = cfg(schedule=[{"days": [0], "start": "12:00", "end": "13:00"}])
        now_cfg, pending = core.plan_config_change(old, new, 0)
        self.assertIsNotNone(pending)
        self.assertTrue(core.schedule_minutes(now_cfg["schedule"]) >= core.schedule_minutes(old["schedule"]))

    def test_delay_uses_current_config(self):
        old = cfg()
        new = cfg(loosen_delay_hours=1, cooldown_base_min=1)
        _, pending = core.plan_config_change(old, new, 0)
        self.assertEqual(pending["apply_at"], 24 * 3600)

    def test_validation_errors(self):
        for bad in ({"sites": []}, {"cooldown_base_min": 0}, {"cooldown_base_min": 30, "cooldown_max_min": 10},
                    {"schedule": [{"days": [], "start": "09:00", "end": "10:00"}]},
                    {"schedule": [{"days": [1], "start": "25:00", "end": "10:00"}]}):
            with self.assertRaises(ValueError):
                core.validate_config({**core.DEFAULT_CONFIG, **bad})


if __name__ == "__main__":
    unittest.main()
