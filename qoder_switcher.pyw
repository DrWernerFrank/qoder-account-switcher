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
        dpi = self.winfo_fpixels("1i")
        scale = dpi / 96.0
        self.tk.call("tk", "scaling", dpi / 72.0)
        self.geometry(f"{int(720 * scale)}x{int(520 * scale)}")
        self.minsize(int(620 * scale), int(440 * scale))
        self.busy = False
        self.current = None

        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family="Segoe UI", size=10)
            except tk.TclError:
                pass

        style = ttk.Style(self)
        style.theme_use("vista")
        style.configure(".", font=("Segoe UI", 10))
        style.configure("TButton", padding=(int(10 * scale), int(6 * scale)))
        style.configure("TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=int(30 * scale))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"),
                        padding=(4, int(6 * scale)))

        top = ttk.LabelFrame(self, text=" Current account ", padding=12)
        top.pack(fill="x", padx=12, pady=(12, 6))
        self.cur_label = ttk.Label(top, text="...", font=("Segoe UI", 12, "bold"))
        self.cur_label.pack(anchor="w")
        self.cur_detail = ttk.Label(top, text="", foreground="#555",
                                    font=("Segoe UI", 10))
        self.cur_detail.pack(anchor="w", pady=(2, 0))

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=5)
        self.b_save = ttk.Button(btns, text="Save current account", command=self.on_save)
        self.b_save.pack(side="left")
        self.b_new = ttk.Button(btns, text="Login new account (browser)", command=self.on_new_login)
        self.b_new.pack(side="left", padx=6)
        self.b_token = ttk.Button(btns, text="Copy API token", command=self.on_copy_token)
        self.b_token.pack(side="left")
        ttk.Button(btns, text="Refresh", command=self.refresh).pack(side="right")

        mid = ttk.LabelFrame(self, text=" Saved accounts ", padding=10)
        mid.pack(fill="both", expand=True, padx=10, pady=5)
        cols = ("email", "name", "saved", "expires")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=8)
        for c, w, t in (("email", 240, "Email"), ("name", 130, "Name"),
                        ("saved", 120, "Saved"), ("expires", 130, "Login valid until")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=int(w * scale), anchor="w")
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

    def set_status(self, text):
        self.status.config(text=text)

    def set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.b_save, self.b_new, self.b_token, self.b_switch, self.b_delete):
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
            tag = "current" if p["email"] == cur_email else ""
            self.tree.insert("", "end", iid=p["email"],
                             values=(p["email"], p["name"], p["saved"], p["refresh_expires"]),
                             tags=(tag,))
        self.tree.tag_configure("current", background="#e8f5e9")

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
    # crisp text on high-DPI displays; must run before the Tk window exists
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    if not QODER_EXE.exists():
        messagebox.showerror("Qoder not found", f"Expected Qoder at:\n{QODER_EXE}")
        sys.exit(1)
    App().mainloop()