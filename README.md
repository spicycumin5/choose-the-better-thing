# FocusGuard

A Windows site blocker that's annoying to bypass on purpose. During your blocked hours, the sites you list are blocked in every browser. To get in during an emergency you have to:

1. **Say why.** You write a real sentence (40+ characters by default). It's logged permanently.
2. **Wait.** The first unlock today costs 5 minutes, the next 10, then 20, 40, and 60 (the cap). You have to stay on the page the whole time. Switch tabs or close it for 15 seconds and the timer starts over.
3. **Get a short window.** At most 15 minutes, and only for the one site. Then it re-blocks itself.

## How it's enforced

A Python daemon runs as **SYSTEM** from a scheduled task. It starts at boot and is relaunched every minute if it stops. Every 5 seconds it rewrites:

- **The hosts file block**, which covers all browsers and apps. Your other entries (Docker etc.) are left alone.
- **The `URLBlocklist` policy for Chrome, Edge and Brave.** It covers every subdomain and incognito, and you can't turn it off from inside the browser. Blocked pages show "blocked by your administrator".

**Making it looser takes 24 hours:** removing a site, shrinking the blocked hours, shortening waits, or uninstalling. Making it stricter applies immediately. You can cancel a pending change from the Settings tab.

## Install

Run this from an **elevated** PowerShell (right-click → Run as administrator):

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

Then open the **FocusGuard** shortcut on your desktop (http://127.0.0.1:8765). Restart your browser once and check `chrome://policy` / `edge://policy`.

Default schedule: Mon–Fri 09:00–22:00 and Sat–Sun 10:00–20:00. Default sites: youtube.com, youtu.be, instagram.com. You can change both in Settings.

## Uninstall

1. In the UI: Settings → **Request uninstall**, then wait out the delay (24h by default).
2. Run `.\uninstall.ps1` as administrator. It refuses to run before the delay is over. It also saves your unlock history to your desktop.

## Try it without installing

```powershell
python focusguard\daemon.py --dry-run --fast
```

This uses a scratch hosts file in `%TEMP%\focusguard-dryrun` and never touches the registry. `--fast` turns every "minute" into one second. Tests: `python -m unittest discover tests`.

## What it can't stop (honestly)

You're an administrator on this PC, so all of these remain possible. Each one takes several deliberate steps, which is the point:

- Deleting the `FocusGuard` scheduled task, or killing `pythonw.exe` from an elevated Task Manager. The task restarts it within about a minute unless the task itself is deleted.
- Editing `C:\ProgramData\FocusGuard\*.json` or the code in `C:\Program Files\FocusGuard` as admin.
- Tampering with your Python install, which lives in your user profile and is what SYSTEM runs.
- Firefox with DNS-over-HTTPS can skip the hosts file. Firefox isn't installed right now. If you add it, ask for Firefox policies to be added.
- Your phone and other devices. For those, use the phone's own screen-time limits or a router/DNS-level blocker.

**If you want it truly hard:** make your daily account a standard (non-admin) user and keep a separate admin account whose password you write down and give to someone else, or put it in a time-locked safe. Then none of the bypasses above are available to you on a whim.

**Antivirus note:** Windows Defender sometimes flags hosts-file changes for popular domains (`SettingsModifier:Win32/HostsFileHijack`). If that happens, allow the detection in Windows Security. The browser policies keep blocking either way.
