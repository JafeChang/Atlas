"""Passwordless login and per-user labels through the real HTTP application."""

import http.cookiejar
import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

from atlas.labels import open_store
from tests.test_webui_entries_render import _entry_forms, running_frontend


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def client():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect()), jar


def request(opener, app, path, data=None, headers=None):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(app.base_url + path, data=body, headers=headers or {})
    try:
        response = opener.open(req, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.status, response.read().decode(), response.headers


def test_login_session_user_labels_and_logout(tmp_path):
    with running_frontend(tmp_path, require_login=True) as world:
        alice, jar = client()
        bob, _ = client()
        status, _, headers = request(alice, world.app, "/feed?order=asc")
        assert status == 303 and headers["Location"].startswith("/login?")
        status, page, _ = request(alice, world.app, headers["Location"])
        assert status == 200 and 'name="name"' in page and "无需密码或注册" not in page
        assert request(alice, world.app, "/health")[0] == 200
        assert request(alice, world.app, "/label", {"actor": "alice"})[0] == 401
        assert request(alice, world.app, "/login", {"name": " "})[0] == 400
        status, _, headers = request(alice, world.app, "/login", {"name": "alice", "return_to": "/feed?order=asc"})
        assert status == 303 and headers["Location"] == "/feed?order=asc"
        assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Lax" in headers["Set-Cookie"]
        assert len(jar) == 1
        status, page, _ = request(alice, world.app, "/feed?order=asc")
        assert status == 200 and "当前用户：alice" in page
        fields = next(f for f in _entry_forms(page) if f["raw_id"] == "raw-feed-render")
        fields.update(actor="bob", label_key="valid", label_value="valid")
        status, result, _ = request(alice, world.app, "/label", fields, {"Accept": "application/json"})
        assert status == 200 and json.loads(result)["values"]["valid"] == "valid"
        with open_store(world.db_path) as store:
            label, = store.all_for(fields["raw_id"])
            assert label.actor == "alice"
        assert request(bob, world.app, "/login", {"name": "bob"})[0] == 303
        status, page, _ = request(bob, world.app, "/feed?labeled=true")
        assert status == 200 and "Alpha &amp; Beta" not in page
        status, page, _ = request(alice, world.app, "/feed?labeled=true")
        assert status == 200 and "Alpha &amp; Beta" in page
        assert request(alice, world.app, "/logout", {"unexpected": "value"})[0] == 400
        assert request(alice, world.app, "/logout", headers={"Origin": "https://evil.example"}, data={})[0] == 403
        # An actual empty POST revokes the session and the previous token becomes invalid.
        token = next(iter(jar)).value
        req = urllib.request.Request(world.app.base_url + "/logout", data=b"")
        try:
            alice.open(req, timeout=10)
        except urllib.error.HTTPError as exc:
            assert exc.code == 303
        assert request(alice, world.app, "/feed")[0] == 303
        assert world.app.server.users.lookup(token) is None
        with sqlite3.connect(world.root / "users.db") as db:
            assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 2
            assert token not in str(db.execute("SELECT * FROM sessions").fetchall())


def test_login_return_and_bad_cookies(tmp_path):
    with running_frontend(tmp_path, require_login=True) as world:
        opener, _ = client()
        assert request(opener, world.app, "/feed", headers={"Cookie": "atlas_session=invalid"})[0] == 303
        status, _, headers = request(opener, world.app, "/login", {"name": "me", "return_to": "//evil.example"})
        assert status == 303 and headers["Location"] == "/feed"
        assert request(opener, world.app, "/feed")[0] == 200


def test_sessions_survive_store_reopen_and_expire(tmp_path):
    from atlas.users import UserStore

    path = tmp_path / "users.db"
    token = UserStore(path).login("me")
    assert UserStore(path).lookup(token) == "me"
    with sqlite3.connect(path) as db:
        db.execute("UPDATE sessions SET expires_at = 0")
    assert UserStore(path).lookup(token) is None
    assert UserStore(path).lookup(UserStore(path).login("me")) == "me"


def test_existing_me_labels_remain_visible(tmp_path):
    with running_frontend(tmp_path, require_login=True) as world:
        world.app.labels.record(raw_id="raw-article-render", label_key="valid", label_value="valid", actor="me")
        opener, _ = client()
        assert request(opener, world.app, "/login", {"name": "me"})[0] == 303
        status, page, _ = request(opener, world.app, "/feed?labeled=true")
        assert status == 200 and "raw-article-render" in page
        other, _ = client()
        assert request(other, world.app, "/login", {"name": "other"})[0] == 303
        status, page, _ = request(other, world.app, "/feed?labeled=true")
        assert status == 200 and "raw-article-render" not in page


def test_active_session_renews_cookie_and_database_before_expiry(tmp_path):
    import time

    from atlas.users import SESSION_SECONDS

    with running_frontend(tmp_path, require_login=True) as world:
        opener, jar = client()
        assert request(opener, world.app, "/login", {"name": "alice"})[0] == 303
        token = next(iter(jar)).value
        now = int(time.time())
        with sqlite3.connect(world.root / "users.db") as db:
            db.execute("UPDATE sessions SET expires_at = ?", (now + 60,))
        status, _, headers = request(opener, world.app, "/feed")
        assert status == 200
        assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Lax" in headers["Set-Cookie"]
        assert next(iter(jar)).value == token
        assert next(iter(jar)).expires >= now + SESSION_SECONDS - 2
        with sqlite3.connect(world.root / "users.db") as db:
            renewed_until, = db.execute("SELECT expires_at FROM sessions").fetchone()
            assert renewed_until >= now + SESSION_SECONDS
        # Subsequent requests keep the cookie aligned with the renewed server expiry.
        status, _, headers = request(opener, world.app, "/feed")
        assert status == 200 and "Set-Cookie" in headers
        with sqlite3.connect(world.root / "users.db") as db:
            assert db.execute("SELECT expires_at FROM sessions").fetchone()[0] == renewed_until
            db.execute("UPDATE sessions SET expires_at = ?", (now - 1,))
        status, _, headers = request(opener, world.app, "/feed")
        assert status == 303 and headers["Location"].startswith("/login?")
        assert "Set-Cookie" not in headers
        with sqlite3.connect(world.root / "users.db") as db:
            assert db.execute("SELECT expires_at FROM sessions").fetchone()[0] == now - 1


def test_legacy_user_ids_and_sessions_migrate_concurrently(tmp_path):
    import time
    from concurrent.futures import ThreadPoolExecutor
    from hashlib import sha256

    from atlas.users import UserStore

    path = tmp_path / "users.db"
    tokens = ("existing-session-a", "existing-session-b")
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE users (name TEXT PRIMARY KEY, created_at INTEGER NOT NULL)")
        db.execute("CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, user_name TEXT NOT NULL, expires_at INTEGER NOT NULL)")
        db.executemany("INSERT INTO users VALUES (?, ?)", [("alice", 1), ("bob", 1)])
        db.executemany("INSERT INTO sessions VALUES (?, ?, ?)", [
            (sha256(token.encode()).hexdigest(), name, int(time.time()) + 60)
            for token, name in zip(tokens, ("alice", "bob"), strict=True)
        ])
    with ThreadPoolExecutor(max_workers=2) as executor:
        sessions = list(executor.map(lambda token: UserStore(path).authenticate(token), tokens))
    assert sessions[0][0] == "alice" and sessions[1][0] == "bob"
    alice_id, bob_id = sessions[0][1], sessions[1][1]
    assert alice_id and bob_id and alice_id != bob_id
    assert UserStore(path).authenticate(tokens[0])[1] == alice_id
    new_token = UserStore(path).login("alice")
    assert UserStore(path).authenticate(new_token)[1] == alice_id
    with sqlite3.connect(path) as db:
        assert dict(db.execute("SELECT name, user_id FROM users")) == {"alice": alice_id, "bob": bob_id}
