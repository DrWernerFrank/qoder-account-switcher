# Qoder Account Switcher

Switch between Qoder accounts instantly on Windows — no password re-entry, no browser round-trip. Save each account's login once, then swap between them with a double-click.

![Windows](https://img.shields.io/badge/platform-Windows-blue) ![Python](https://img.shields.io/badge/python-3.10%2B-green)

## How it works

Qoder stores its login (access token, refresh token, account identity) in an encrypted file:

```
%APPDATA%\com.qoder.app.stable\auth.v1.dat
```

The file is encrypted with Chromium's `os_crypt` scheme: an AES-256-GCM key wrapped by Windows DPAPI (`Local State` → `os_crypt.encrypted_key`). Because DPAPI is bound to your Windows user, the same user can decrypt and re-use the blob — which is exactly what this tool does:

1. **Save** — copies the live `auth.v1.dat` into a named profile folder and reads the account email/name from the decrypted blob for labeling.
2. **Switch** — closes Qoder, swaps in the selected profile's `auth.v1.dat`, and restarts Qoder already signed in.
3. **New login** — saves the current login, clears `auth.v1.dat`, and restarts Qoder at the sign-in screen (browser login or code).

Before every switch or logout the current login is auto-saved back to its profile, so refreshed tokens are never lost.

## Features

- Current account display (name, email, token expiry)
- Save / switch / delete account profiles
- One-click "login new account via browser" flow
- Copy the current API access token to clipboard
- Login validity (refresh-token expiry) shown per profile
- Double-click a profile to switch

## Requirements

- Windows (DPAPI is Windows-only)
- Qoder desktop installed at the default location (`%LOCALAPPDATA%\Programs\Qoder\Qoder.exe`)
- Python 3.10+ with one dependency:

```
pip install cryptography
```

## Usage

Double-click `qoder_switcher.pyw`, or run:

```
python qoder_switcher.pyw
```

1. Click **Save current account** to store the account you're logged into.
2. Click **Login new account (browser)**, sign in with your other account, then **Refresh** → **Save current account**.
3. From then on, select a profile and click **Switch to selected** (or double-click it).

> Switching closes Qoder, so any running agent sessions stop. The app asks for confirmation first.

## Security notes

- Saved profiles live in `profiles/` next to the script and contain **real login tokens**. The folder is gitignored — never commit or share it.
- The blobs stay encrypted with your Windows user's DPAPI key. They cannot be decrypted on another PC or by another Windows user.
- The tool only reads/writes files belonging to your own Qoder installation; it makes no network requests.

## License

MIT