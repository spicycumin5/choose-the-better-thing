"""System side effects: hosts file, browser policies, DNS cache."""
import logging
import subprocess

from core import expand_hosts, render_hosts

log = logging.getLogger("focusguard")

REAL_HOSTS = r"C:\Windows\System32\drivers\etc\hosts"

# Chromium-based browsers read URLBlocklist from these keys. A list policy is
# stored as values named "1", "2", ... so FocusGuard owns each key entirely.
POLICY_KEYS = (
    r"SOFTWARE\Policies\Google\Chrome\URLBlocklist",
    r"SOFTWARE\Policies\Microsoft\Edge\URLBlocklist",
    r"SOFTWARE\Policies\BraveSoftware\Brave\URLBlocklist",
)


class Enforcer:
    def __init__(self, hosts_path=REAL_HOSTS, use_registry=True, flush_dns=True):
        self.hosts_path = hosts_path
        self.use_registry = use_registry
        self.flush_dns = flush_dns

    def apply(self, sites):
        """Make the system block exactly `sites`. Returns True if anything changed."""
        changed = self._apply_hosts(expand_hosts(sites))
        if self.use_registry:
            for key in POLICY_KEYS:
                changed |= self._apply_policy(key, sorted(sites))
        if changed and self.flush_dns:
            subprocess.run(["ipconfig", "/flushdns"], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return changed

    # ------------------------------------------------------------ hosts

    def _apply_hosts(self, domains):
        try:
            with open(self.hosts_path, encoding="utf-8", errors="surrogateescape") as f:
                current = f.read()
        except FileNotFoundError:
            current = ""
        desired = render_hosts(current, domains)
        if desired == current:
            return False
        try:
            # Write in place (not replace) so the file keeps its ACL and attributes.
            with open(self.hosts_path, "w", encoding="utf-8", errors="surrogateescape",
                      newline="\r\n") as f:
                f.write(desired)
        except OSError as e:
            # Antivirus sometimes holds the file briefly; the next tick retries.
            log.warning("Could not write hosts file: %s", e)
            return False
        log.info("Hosts file updated (%d blocked names)", len(domains))
        return True

    # ------------------------------------------------------------ registry

    def _apply_policy(self, key_path, sites):
        import winreg
        if self._read_list(winreg, key_path) == sites:
            return False
        access = winreg.KEY_ALL_ACCESS | winreg.KEY_WOW64_64KEY
        if not sites:
            try:
                winreg.DeleteKeyEx(winreg.HKEY_LOCAL_MACHINE, key_path, winreg.KEY_WOW64_64KEY)
            except FileNotFoundError:
                pass
        else:
            with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, key_path, 0, access) as key:
                names = []
                try:
                    i = 0
                    while True:
                        names.append(winreg.EnumValue(key, i)[0])
                        i += 1
                except OSError:
                    pass
                for name in names:
                    winreg.DeleteValue(key, name)
                for i, site in enumerate(sites, 1):
                    winreg.SetValueEx(key, str(i), 0, winreg.REG_SZ, site)
        log.info("Policy %s set to %s", key_path, sites)
        return True

    @staticmethod
    def _read_list(winreg, key_path):
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path, 0,
                                 winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        except FileNotFoundError:
            return []
        values = []
        with key:
            count = winreg.QueryInfoKey(key)[1]
            for i in range(1, count + 1):
                try:
                    values.append(winreg.QueryValueEx(key, str(i))[0])
                except FileNotFoundError:
                    return None  # unexpected layout: force a rewrite
        return values
