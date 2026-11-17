"""Qoder Account Switcher — save, switch, and re-login Qoder accounts instantly."""

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, ttk

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

APPDATA = Path(os.environ["APPDATA"]) / "com.qoder.app.stable"
AUTH_FILE = APPDATA / "auth.v1.dat"
LOCAL_STATE = APPDATA / "Local State"
QODER_EXE = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Qoder" / "Qoder.exe"
PROFILES_DIR = Path(__file__).parent / "profiles"
NO_WINDOW = 0x08000000
API_HOST = "https://openapi.qoder.sh"
BASE_W, BASE_H = 760, 520


def enable_dpi_awareness():
    """Render at native resolution instead of letting Windows stretch us.

    Without this, on any display scaling above 100% the whole window is
    bitmap-scaled and every glyph comes out soft.
    """
    try:  # Windows 10 1703+: per-monitor v2
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:  # Windows 8.1+
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:  # Vista+
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


enable_dpi_awareness()


# ---------- crypto / auth ----------

class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def dpapi_unprotect(data: bytes) -> bytes:
    blob_in = DATA_BLOB(len(data), ctypes.create_string_buffer(data, len(data)))
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError("DPAPI decryption failed (profiles only work on the Windows user that saved them).")
    out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return out


def load_aes_key() -> bytes:
    state = json.loads(LOCAL_STATE.read_text(encoding="utf-8"))
    enc = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if not enc.startswith(b"DPAPI"):
        raise ValueError("Unexpected Local State key format")
    return dpapi_unprotect(enc[5:])


def read_auth(path: Path):
    if not path.exists():
        return None
    blob = path.read_bytes()
    if not blob.startswith(b"v10"):
        raise ValueError("Unexpected auth file format")
    plain = AESGCM(load_aes_key()).decrypt(blob[3:15], blob[15:], None)
    return json.loads(plain)


def fmt_expiry(value):
    if not value:
        return "?"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(value)


# ---------- live account state ----------

def api_get(path: str, token: str, timeout=15):
    req = urllib.request.Request(
        API_HOST + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Cosy-ClientType": "10",
            "Cosy-Version": "0.3.4",
            "User-Agent": "Qoder",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def quota_state(auth_path: Path):
    """Ask Qoder's server what this account is actually entitled to.

    The daily/free credits are granted server-side against a per-machine risk
    identity, so this is the only honest source of truth. Read-only GETs.
    """
    auth = read_auth(auth_path)
    token = auth.get("token")
    if not token:
        return {"err": "no access token"}
    state = {"plan": "?", "remaining": None, "total": None,
             "exceeded": None, "campaign": "?", "daily": "?", "err": None}
    try:
        usage = api_get("/sash/api/v2/me/usage", token).get("qoderUsage") or {}
        addon = usage.get("addOnQuota") or {}
        plan_q = usage.get("userQuota") or {}
        remaining = addon.get("remaining")
        total = addon.get("total")
        if remaining is None:  # no add-on pack -> plan quota is the only balance
            remaining, total = plan_q.get("remaining"), plan_q.get("total")
        user_type = usage.get("userType") or "?"
        state.update({
            "plan": "Free" if user_type == "personal_standard" else user_type,
            "remaining": remaining, "total": total,
            "exceeded": usage.get("isQuotaExceeded"),
        })
    except urllib.error.HTTPError as e:
        state["err"] = "401 expired" if e.code in (401, 403) else f"HTTP {e.code}"
    except Exception as e:
        state["err"] = type(e).__name__
    try:
        camp = api_get("/sash/api/v1/me/campaigns", token)
        items = camp.get("campaigns") or []
        status = items[0].get("claimStatus") if items else None
        state["campaign"] = "claimable" if camp.get("claimable") else (status or "-").lower()
        # The daily 100-credit reward is delivered as a CLAIM_BENEFIT campaign.
        benefit = [c for c in items if c.get("actionType") == "CLAIM_BENEFIT"]
        marks = [(c.get("claimStatus") or "").upper() for c in benefit]
        if "CLAIMABLE" in marks:
            state["daily"] = "claimable"
        elif marks:
            state["daily"] = "claimed"
        else:
            state["daily"] = "not offered"
    except Exception:
        pass
    return state


def fmt_credit(state):
    if state.get("err"):
        return state["err"]
    rem, total = state.get("remaining"), state.get("total")
    if rem is None:
        return "?"
    return f"{rem}/{total}" if total else str(rem)


# ---------- profiles ----------

def profile_dir(email: str) -> Path:
    safe = re.sub(r'[<>:"/\\|?*]', "_", email)
    return PROFILES_DIR / safe


def list_profiles():
    out = []
    if PROFILES_DIR.exists():
        for d in sorted(PROFILES_DIR.iterdir()):
            blob = d / "auth.v1.dat"
            if not blob.is_file():
                continue
            info = {"dir": d, "email": d.name, "name": "", "saved": "", "refresh_expires": "?"}
            meta = d / "meta.json"
            if meta.exists():
                try:
                    m = json.loads(meta.read_text(encoding="utf-8"))
                    info.update({k: m.get(k, info[k]) for k in ("name", "saved", "refresh_expires")})
                except (json.JSONDecodeError, OSError):
                    pass
            out.append(info)
    return out


def save_profile(email: str, name: str, refresh_expires: str):
    d = profile_dir(email)
    d.mkdir(parents=True, exist_ok=True)
    shutil.copy2(AUTH_FILE, d / "auth.v1.dat")
    meta = {
        "email": email,
        "name": name,
        "saved": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "refresh_expires": fmt_expiry(refresh_expires),
    }
    (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return d


def autosave_current():
    """Keep the live account's stored copy fresh before any switch/logout."""
    try:
        auth = read_auth(AUTH_FILE)
    except Exception:
        return
    if auth and auth.get("user", {}).get("email"):
        u = auth["user"]
        save_profile(u["email"], u.get("name") or "", auth.get("refreshTokenExpiresAt"))


# ---------- process control ----------

def qoder_running() -> bool:
    r = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq Qoder.exe", "/NH"],
        capture_output=True, text=True, creationflags=NO_WINDOW,
    )
    return "Qoder.exe" in r.stdout


def kill_qoder(timeout=20):
    subprocess.run(
        ["taskkill", "/F", "/T", "/IM", "Qoder.exe"],
        capture_output=True, creationflags=NO_WINDOW,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not qoder_running():
            time.sleep(0.7)  # let file handles close
            return True
        time.sleep(0.4)
    return not qoder_running()


def start_qoder():
    subprocess.Popen([str(QODER_EXE)], cwd=str(QODER_EXE.parent), close_fds=True)


def copy_with_retry(src: Path, dst: Path, tries=8):
    for i in range(tries):
        try:
            shutil.copy2(src, dst)
            return
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.5)


# ---------- GUI ----------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Qoder Account Switcher")
        self.busy = False
        self.current = None
        self.quota = {}          # email -> state dict from the server
        self.quota_busy = False
        self._dpi = None
        self._apply_scaling()
        self.bind("<Configure>", self._on_dpi_change)

        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family="Segoe UI", size=10)
            except tk.TclError:
                pass

        style = ttk.Style(self)
        style.theme_use("vista")
        style.configure(".", font=("Segoe UI", 10))
        style.configure("TButton", padding=(int(10 * self.scale), int(6 * self.scale)))
        style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=int(30 * self.scale))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"),
                        padding=(4, int(6 * self.scale)))

        top = ttk.LabelFrame(self, text=" Current account ", padding=10)
        top.pack(fill="x", padx=10, pady=(10, 5))
        self.cur_label = ttk.Label(top, text="...", font=("Segoe UI", 12, "bold"))
        self.cur_label.pack(anchor="w")
        self.cur_detail = ttk.Label(top, text="", foreground="#555", font=("Segoe UI", 10))
        self.cur_detail.pack(anchor="w", pady=(2, 0))
        self.cur_credit = ttk.Label(top, text="Credits: checking...", foreground="#555",
                                    font=("Segoe UI", 10))
        self.cur_credit.pack(anchor="w")

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=5)
        self.b_save = ttk.Button(btns, text="Save current account", command=self.on_save)
        self.b_save.pack(side="left")
        self.b_new = ttk.Button(btns, text="Login new account (browser)", command=self.on_new_login)
        self.b_new.pack(side="left", padx=6)
        self.b_token = ttk.Button(btns, text="Copy API token", command=self.on_copy_token)
        self.b_token.pack(side="left")
        self.b_quota = ttk.Button(btns, text="Check credits", command=lambda: self.fetch_quota(force=True))
        self.b_quota.pack(side="left", padx=6)
        ttk.Button(btns, text="Refresh", command=self.refresh).pack(side="right")

        mid = ttk.LabelFrame(self, text=" Saved accounts ", padding=10)
        mid.pack(fill="both", expand=True, padx=10, pady=5)
        cols = ("email", "name", "plan", "credits", "daily", "saved", "expires")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=8)
        for c, w, t in (("email", 205, "Email"), ("name", 85, "Name"),
                        ("plan", 55, "Plan"), ("credits", 80, "Credits"),
                        ("daily", 85, "Daily 100"), ("saved", 90, "Saved"),
                        ("expires", 105, "Valid until")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=int(w * self.scale), anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.on_switch())

        row = ttk.Frame(self)
        row.pack(fill="x", padx=10, pady=(0, 5))
        self.b_switch = ttk.Button(row, text="Switch to selected  (double-click works too)", command=self.on_switch)
        self.b_switch.pack(side="left")
        self.b_delete = ttk.Button(row, text="Delete selected", command=self.on_delete)
        self.b_delete.pack(side="left", padx=6)

        self.status = ttk.Label(self, text="Profiles are encrypted for this Windows user only.",
                                foreground="#777", padding=(10, 4))
        self.status.pack(fill="x", side="bottom")

        self.refresh()

    # -- helpers --

    def _apply_scaling(self):
        """Scale the window and point-sized fonts to the monitor's real DPI."""
        try:
            dpi = float(self.winfo_fpixels("1i"))
        except (tk.TclError, ValueError):
            dpi = 96.0
        self._dpi = dpi
        self.tk.call("tk", "scaling", dpi / 72.0)
        self.scale = dpi / 96.0
        scale = self.scale
        self.geometry(f"{int(BASE_W * scale)}x{int(BASE_H * scale)}")
        self.minsize(int(BASE_W * 0.9 * scale), int(BASE_H * 0.85 * scale))

    def _on_dpi_change(self, event):
        """Re-derive scaling when the window lands on a monitor with other DPI."""
        if event.widget is not self or not hasattr(self, "tree"):
            return
        try:
            dpi = float(self.winfo_fpixels("1i"))
        except (tk.TclError, ValueError):
            return
        if self._dpi is None or abs(dpi - self._dpi) < 6:
            return
        self._dpi = dpi
        self.scale = dpi / 96.0
        self.tk.call("tk", "scaling", dpi / 72.0)
        self._resize_columns()

    def _resize_columns(self):
        widths = {"email": 205, "name": 85, "plan": 55, "credits": 80,
                  "daily": 85, "saved": 90, "expires": 105}
        for c, w in widths.items():
            self.tree.column(c, width=int(w * self.scale))

    def set_status(self, text):
        self.status.config(text=text)

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.b_save, self.b_new, self.b_token, self.b_quota, self.b_switch, self.b_delete):
            b.config(state=state)

    def run_bg(self, fn):
        def wrapper():
            try:
                fn()
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Error", str(e)))
            finally:
                self.after(0, lambda: self.set_busy(False))
                self.after(0, self.refresh)
        self.set_busy(True)
        threading.Thread(target=wrapper, daemon=True).start()

    def refresh(self):
        try:
            self.current = read_auth(AUTH_FILE)
        except Exception as e:
            self.current = None
            self.set_status(f"Could not read auth: {e}")
        if self.current:
            u = self.current.get("user", {})
            self.cur_label.config(text=f"{u.get('name') or '?'}  <{u.get('email') or '?'}>")
            self.cur_detail.config(text=f"Access token expires: {fmt_expiry(self.current.get('expiresAt'))}")
        else:
            self.cur_label.config(text="Not logged in")
            self.cur_detail.config(text="Use 'Login new account' to sign in via browser.")
        for item in self.tree.get_children():
            self.tree.delete(item)
        cur_email = (self.current or {}).get("user", {}).get("email")
        for p in list_profiles():
            q = self.quota.get(p["email"]) or {}
            tag = "current" if p["email"] == cur_email else ""
            self.tree.insert("", "end", iid=p["email"],
                             values=(p["email"], p["name"], q.get("plan") or "...",
                                     fmt_credit(q) if q else "...", q.get("daily") or "...",
                                     p["saved"], p["refresh_expires"]),
                             tags=(tag,))
        self.tree.tag_configure("current", background="#e8f5e9")
        self.fetch_quota()

    def fetch_quota(self, force=False):
        """Read plan + remaining credits for every saved profile from Qoder's API."""
        if self.quota_busy:
            if force:
                self.set_status("Credit check already running...")
            return
        self.quota_busy = True
        profiles = list_profiles()
        has_current = bool(self.current and self.current.get("user", {}).get("email"))

        def job():
            results = {}
            for p in profiles:
                try:
                    results[p["email"]] = quota_state(p["dir"] / "auth.v1.dat")
                except Exception as e:
                    results[p["email"]] = {"err": type(e).__name__}
            cur = None
            if has_current:
                try:
                    cur = quota_state(AUTH_FILE)
                except Exception as e:
                    cur = {"err": type(e).__name__}
            self.after(0, lambda: self.apply_quota(results, cur))

        threading.Thread(target=job, daemon=True).start()

    def apply_quota(self, results, cur):
        self.quota.update(results)
        for email, st in results.items():
            if self.tree.exists(email):
                self.tree.set(email, "plan", st.get("plan") or "?")
                self.tree.set(email, "credits", fmt_credit(st))
                self.tree.set(email, "daily", st.get("daily") or "?")
        if cur is not None:
            if cur.get("err"):
                self.cur_credit.config(text=f"Credits: cannot read ({cur['err']})", foreground="#b00020")
            else:
                notes = []
                if cur.get("exceeded"):
                    notes.append("quota exceeded")
                if cur.get("campaign") not in (None, "?", "-", "claimed"):
                    notes.append(f"campaign {cur['campaign']}")
                text = f"Credits: {fmt_credit(cur)}  ({cur.get('plan')})"
                if notes:
                    text += "  — " + ", ".join(notes)
                self.cur_credit.config(text=text, foreground="#b00020" if cur.get("exceeded") else "#555")
        self.quota_busy = False
        self.set_status("Credits fetched from Qoder's server (read-only).")

    def selected_email(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    # -- actions --

    def on_save(self):
        if not self.current:
            messagebox.showinfo("Save", "No account is currently logged in.")
            return
        u = self.current["user"]
        save_profile(u["email"], u.get("name") or "", self.current.get("refreshTokenExpiresAt"))
        self.set_status(f"Saved {u['email']}")
        self.refresh()

    def on_copy_token(self):
        if not self.current:
            messagebox.showinfo("Token", "No account is currently logged in.")
            return
        self.clipboard_clear()
        self.clipboard_append(self.current["token"])
        self.set_status("Access token copied to clipboard (valid until expiry shown above).")

    def on_switch(self):
        email = self.selected_email()
        if not email:
            messagebox.showinfo("Switch", "Select a saved account first.")
            return
        if self.current and self.current.get("user", {}).get("email") == email:
            self.set_status("Already logged in as that account.")
            return
        if not messagebox.askyesno(
            "Switch account",
            f"Switch to {email}?\n\nQoder will be CLOSED (any running agent sessions stop), "
            "the login is swapped, and Qoder restarts automatically.",
        ):
            return

        def job():
            self.after(0, lambda: self.set_status("Saving current login..."))
            autosave_current()
            self.after(0, lambda: self.set_status("Closing Qoder..."))
            if qoder_running() and not kill_qoder():
                raise RuntimeError("Could not close Qoder. Close it manually and try again.")
            self.after(0, lambda: self.set_status("Swapping login..."))
            copy_with_retry(profile_dir(email) / "auth.v1.dat", AUTH_FILE)
            self.after(0, lambda: self.set_status("Starting Qoder..."))
            start_qoder()
            self.after(0, lambda: self.set_status(f"Switched to {email}"))

        self.run_bg(job)

    def on_new_login(self):
        if not messagebox.askyesno(
            "New login",
            "Log in with a different account via browser?\n\n"
            "The current login is saved first, Qoder is CLOSED, and it restarts "
            "at the sign-in screen (browser login or code).",
        ):
            return

        def job():
            self.after(0, lambda: self.set_status("Saving current login..."))
            autosave_current()
            self.after(0, lambda: self.set_status("Closing Qoder..."))
            if qoder_running() and not kill_qoder():
                raise RuntimeError("Could not close Qoder. Close it manually and try again.")
            self.after(0, lambda: self.set_status("Clearing login..."))
            for i in range(8):
                try:
                    AUTH_FILE.unlink(missing_ok=True)
                    break
                except PermissionError:
                    if i == 7:
                        raise
                    time.sleep(0.5)
            self.after(0, lambda: self.set_status("Starting Qoder — sign in via browser..."))
            start_qoder()
            self.after(0, lambda: self.set_status("Sign in, then click Refresh and Save to keep the new account."))

        self.run_bg(job)

    def on_delete(self):
        email = self.selected_email()
        if not email:
            return
        if messagebox.askyesno("Delete", f"Remove saved profile {email}?\n(The live login is not affected.)"):
            shutil.rmtree(profile_dir(email), ignore_errors=True)
            self.refresh()


if __name__ == "__main__":
    if not QODER_EXE.exists():
        messagebox.showerror("Qoder not found", f"Expected Qoder at:\n{QODER_EXE}")
        sys.exit(1)
    App().mainloop()