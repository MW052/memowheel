"""Secure storage for the LLM API key.

Prefers the OS keychain - on Windows the Credential Locker via `keyring`'s
WinVaultKeyring backend, which is DPAPI-encrypted per user account - and falls
back to the plaintext `settings.json` only when no real keyring backend is
available (package missing, a headless/misconfigured box, or a frozen build
whose backend didn't bundle). The fallback is reported (`storage_mode`) so the
UI can tell the user their key is NOT encrypted at rest.

Read precedence: the `personal_gpt_apikey` env var (an unmanaged override for
power users) > the OS keychain (managed default) > plaintext settings (last-resort
compatibility, and the legacy location we migrate away from). Only ONE stored
copy is kept: writing to the keychain scrubs any plaintext copy, and a legacy
plaintext key is migrated into the keychain on first read.
"""

import os

import config

_SERVICE = "TripVideo"
_ACCOUNT = "llm_api_key"
_SETTING = "llm_api_key"  # legacy plaintext location in settings.json
_ENV = "personal_gpt_apikey"


def _keyring():
    """The `keyring` module if a real (non-fail) backend is available, else None.

    Imported lazily and defensively: a missing package OR a fail/null backend
    (the sentinel keyring installs when it finds nothing usable) both degrade to
    the plaintext path rather than crashing the app."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as _Fail
        if isinstance(keyring.get_keyring(), _Fail):
            return None
        return keyring
    except Exception:
        return None


def keychain_available() -> bool:
    return _keyring() is not None


def get_api_key() -> str:
    """The API key, by precedence: env override > keychain > plaintext settings.

    Side effect: if a legacy plaintext key exists and the keychain is available
    but empty, migrate it into the keychain and scrub the plaintext copy (runs
    once, idempotent)."""
    env = os.environ.get(_ENV, "")
    if env:
        return env

    plaintext = config.get_setting(_SETTING, "") or ""
    kr = _keyring()
    if kr is not None:
        try:
            stored = kr.get_password(_SERVICE, _ACCOUNT)
        except Exception:
            stored = None
        if stored:
            return stored
        if plaintext:  # migrate legacy plaintext -> keychain, then scrub
            try:
                kr.set_password(_SERVICE, _ACCOUNT, plaintext)
                config.set_setting(_SETTING, "")
                return plaintext
            except Exception:
                pass  # migration failed; keep serving the plaintext value
    return plaintext


def set_api_key(value: str) -> bool:
    """Store the key. Returns True if it went to the OS keychain (encrypted at
    rest), False if it fell back to plaintext settings.json. Keeps a single copy:
    a keychain write scrubs any plaintext copy, and the plaintext fallback clears
    the keychain entry, so the key never lingers in two places."""
    value = (value or "").strip()
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(_SERVICE, _ACCOUNT, value)
            if config.get_setting(_SETTING, ""):
                config.set_setting(_SETTING, "")
            return True
        except Exception:
            pass
    # plaintext fallback: clear any keychain entry so there's one source of truth
    if kr is not None:
        try:
            kr.delete_password(_SERVICE, _ACCOUNT)
        except Exception:
            pass
    config.set_setting(_SETTING, value)
    return False


def has_api_key() -> bool:
    return bool(get_api_key())


def storage_mode() -> str:
    """Where the key currently lives, for display: 'env', 'keychain',
    'plaintext', or 'none'."""
    if os.environ.get(_ENV, ""):
        return "env"
    kr = _keyring()
    if kr is not None:
        try:
            if kr.get_password(_SERVICE, _ACCOUNT):
                return "keychain"
        except Exception:
            pass
    if config.get_setting(_SETTING, ""):
        return "plaintext"
    return "none"
