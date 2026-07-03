#!/usr/bin/env python3
"""Lark (international) desktop notifications -> Feishu bot DM forwarder.

Reads the macOS Notification Center DB, picks up notifications posted by the
international Lark desktop app (com.larksuite.larkapp), and forwards them in
real time to a Feishu user via a Feishu bot DM (open.feishu.cn).

Why this works for "only un-muted chats": the Lark desktop client already
applies the account-synced Do-Not-Disturb / mute settings, so muted chats
never post a macOS notification in the first place. We just relay whatever
the OS notification center received.

Config (secret) lives outside git at ~/.claude/lark_notif_forwarder/config.json.
State (watermark) at ~/.claude/lark_notif_forwarder/state.json.
"""
import json
import os
import plistlib
import sqlite3
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".claude", "lark_notif_forwarder")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
NOTIF_DB = os.path.join(
    HOME, "Library/Group Containers/group.com.apple.usernoted/db2/db"
)

MAX_SEND_RETRIES = 5         # per-notification before giving up & skipping
SEND_SPACING = 0.3          # seconds between sends (gentle rate limit)
HEARTBEAT_EVERY = 600       # log a heartbeat every N seconds
BODY_MAX = 800              # truncate very long bodies


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


# ---------------------------------------------------------------- Feishu API
_token = {"value": None, "exp": 0.0}


def get_token(cfg):
    now = time.time()
    if _token["value"] and now < _token["exp"] - 60:
        return _token["value"]
    body = json.dumps(
        {"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]}
    ).encode()
    req = urllib.request.Request(
        cfg["feishu_base"] + "/open-apis/auth/v3/tenant_access_token/internal",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        d = json.loads(resp.read())
    if d.get("code") != 0:
        raise RuntimeError(f"token error: {d}")
    _token["value"] = d["tenant_access_token"]
    _token["exp"] = now + float(d.get("expire", 7200))
    return _token["value"]


def send_dm(cfg, text):
    token = get_token(cfg)
    body = json.dumps(
        {
            "receive_id": cfg["receive_id"],
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
    ).encode()
    rid_type = cfg.get("receive_id_type", "open_id")
    url = cfg["feishu_base"] + f"/open-apis/im/v1/messages?receive_id_type={rid_type}"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        d = json.loads(resp.read())
    if d.get("code") != 0:
        raise RuntimeError(f"send error: {d}")
    return d


# ----------------------------------------------------------- notification DB
def _open_db():
    return sqlite3.connect(f"file:{NOTIF_DB}?mode=ro", uri=True, timeout=5)


def current_max_rec_id():
    con = _open_db()
    try:
        row = con.execute("SELECT MAX(rec_id) FROM record").fetchone()
        return row[0] or 0
    finally:
        con.close()


def fetch_new(cfg, last_rec_id):
    bundles = cfg["target_bundles"]
    placeholders = ",".join("?" * len(bundles))
    sql = (
        "SELECT r.rec_id, a.identifier, r.data "
        "FROM record r JOIN app a ON r.app_id = a.app_id "
        f"WHERE a.identifier IN ({placeholders}) AND r.rec_id > ? "
        "ORDER BY r.rec_id ASC"
    )
    con = _open_db()
    try:
        return con.execute(sql, (*bundles, last_rec_id)).fetchall()
    finally:
        con.close()


def _as_text(v):
    """Notification fields are usually plain strings; system/localized ones can
    be arrays. Only accept real strings."""
    return v if isinstance(v, str) and v.strip() else None


def decode_notif(data):
    try:
        pl = plistlib.loads(data)
    except Exception:
        return None
    req = pl.get("req", {}) if isinstance(pl, dict) else {}
    titl = _as_text(req.get("titl"))
    subt = _as_text(req.get("subt"))
    body = _as_text(req.get("body"))
    if not (titl or body):
        return None
    return titl, subt, body


def format_msg(titl, subt, body):
    head = titl or "Lark"
    if subt:
        head = f"{head} · {subt}"
    text = body or ""
    if len(text) > BODY_MAX:
        text = text[:BODY_MAX] + "…"
    return f"🔔 {head}\n{text}".rstrip()


# ------------------------------------------------------------------- runtime
def main():
    cfg = load_config()
    log(f"starting; bundles={cfg['target_bundles']} receive={cfg['receive_id']}")

    state = load_state()
    last = state.get("last_rec_id")
    if last is None:
        # first run: skip history, only forward notifications from now on
        last = current_max_rec_id()
        save_state({"last_rec_id": last})
        log(f"first run, watermark set to current max rec_id={last}")

    fails = {}
    last_beat = time.time()
    poll = float(cfg.get("poll_interval", 2.0))

    while True:
        try:
            mx = current_max_rec_id()
            if mx < last:  # notification DB was rebuilt / rec_id reset
                log(f"rec_id reset detected (max={mx} < last={last}); realigning")
                last = mx
                save_state({"last_rec_id": last})

            for rec_id, ident, data in fetch_new(cfg, last):
                decoded = decode_notif(data)
                if decoded is None:
                    last = rec_id
                    save_state({"last_rec_id": last})
                    continue
                msg = format_msg(*decoded)
                try:
                    send_dm(cfg, msg)
                    fails.pop(rec_id, None)
                    last = rec_id
                    save_state({"last_rec_id": last})
                    time.sleep(SEND_SPACING)
                except Exception as e:
                    n = fails.get(rec_id, 0) + 1
                    fails[rec_id] = n
                    log(f"send failed rec={rec_id} attempt={n}: {e}")
                    if n >= MAX_SEND_RETRIES:
                        log(f"giving up on rec={rec_id}, skipping")
                        fails.pop(rec_id, None)
                        last = rec_id
                        save_state({"last_rec_id": last})
                    else:
                        break  # retry whole batch next poll, watermark not moved
        except Exception as e:
            log(f"loop error: {e}")

        now = time.time()
        if now - last_beat >= HEARTBEAT_EVERY:
            log(f"alive; watermark={last}")
            last_beat = now
        time.sleep(poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"fatal: {e}")
        sys.exit(1)
