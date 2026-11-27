"""Post rendered clips to Instagram (Reels) and Facebook (Page Reels) automatically.

Runs on GitHub Actions after every Tracker run (`social.yml`, ~every 6 h) + manual button.
A Batch row is posted when:
  - render_status = done and approved is not "no"  (owner's veto: set approved = no),
  - it was rendered at least `social.veto_hours` ago (time to glance at it / veto it),
  - that platform isn't done yet for the row (ig_status / fb_status),
  - the brand has a token in the secret META_TOKENS_JSON,
  - at most `social.max_per_brand_per_run` per brand per run (spreads posts over the day).
The MP4 is read from Google Drive with the service account (owner shares the "GTA6 Clipper"
Drive folder with the service account email as Viewer) and uploaded straight to Meta
(resumable upload on rupload.facebook.com: no public link needed).
Caption = Batch.ig_caption (Instagram and Facebook). Results -> Batch ig_status / ig_url /
fb_status / fb_url / social_at, and Posted rows `<batch_id>-instagram` / `<batch_id>-facebook`.

META_TOKENS_JSON (GitHub secret, never printed), either Meta's me/accounts answer pasted as-is
({"data": [{"name": "Wasted Again", "id": ..., "access_token": ..., "instagram_business_account": ...}]},
Pages matched to brands by name) or
{"wasted_again": {"ig_user_id": "1784...", "page_id": "1093...", "token": "<Page access token>"}, ...}
A Page token made from a long-lived user token does not expire. Missing page_id -> no Facebook.

Run:  python -m agents.post_social [--dry-run]
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from lib import sheets
from lib.config import load_settings
from lib.schema import TABS

GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com"
ENV = "META_TOKENS_JSON"


class MetaError(Exception):
    pass


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _norm(x):
    return "".join(ch for ch in str(x).lower() if ch.isalnum())


def tokens_from_accounts(data, brands):
    """Meta's `me/accounts?fields=name,id,access_token,instagram_business_account` answer, pasted
    as-is into the secret -> {brand_key: {...}}. A Page belongs to the brand whose name (or key)
    its name contains, e.g. Page "Wasted Again Clips" -> wasted_again."""
    out = {}
    for page in data.get("data") or []:
        name = _norm(page.get("name", ""))
        for b in brands:
            if b["key"] in out or not page.get("access_token"):
                continue
            if _norm(b.get("name", "")) in name or _norm(b["key"]) in name:
                out[b["key"]] = {"token": page["access_token"], "page_id": str(page.get("id", "")),
                                 "ig_user_id": str((page.get("instagram_business_account") or {}).get("id", ""))}
                break
    return out


def load_tokens(env=None, brands=None):
    """Secret META_TOKENS_JSON in either form: {brand_key: {token, page_id, ig_user_id}} or Meta's
    me/accounts answer copied straight from Graph API Explorer ({"data": [...pages...]})."""
    raw = (os.environ if env is None else env).get(ENV, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise MetaError(f"secret {ENV} is not valid JSON (copy Meta's whole answer, from {{ to }})")
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        return tokens_from_accounts(data, brands if brands is not None else load_settings()["brands"])
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("token")}


def due(batch_rows, tokens, cfg, now):
    """[(row, platform)] to post this run, oldest render first, capped per brand."""
    veto = timedelta(hours=cfg.get("veto_hours", 2))
    max_age = timedelta(hours=cfg.get("max_age_hours", 48))
    per_brand, todo = {}, []
    rows = [r for r in batch_rows if r.get("render_status") == "done"
            and str(r.get("approved", "")).strip().lower() != "no" and r.get("brand") in tokens]
    rows.sort(key=lambda r: str(r.get("rendered_at", "")))
    for r in rows:
        t = parse_time(r.get("rendered_at"))
        if not t or now - t < veto or now - t > max_age:
            continue
        tok = tokens[r["brand"]]
        plats = []
        if cfg.get("instagram", True) and tok.get("ig_user_id") and r.get("ig_status") != "done":
            plats.append("instagram")
        if cfg.get("facebook", True) and tok.get("page_id") and r.get("fb_status") != "done":
            plats.append("facebook")
        if not plats:
            continue
        if per_brand.get(r["brand"], 0) >= cfg.get("max_per_brand_per_run", 2):
            continue
        per_brand[r["brand"]] = per_brand.get(r["brand"], 0) + 1
        todo += [(r, p) for p in plats]
    return todo


def caption_of(row):
    cap = str(row.get("ig_caption") or "").strip()
    if not cap:
        cap = f"{row.get('title', '')}\n🎥 {row.get('creator_credit', '')}\n\n{row.get('hashtags', '')}"
    return cap[:2200]


# --- Meta API --------------------------------------------------------------------------------

class Meta:
    def __init__(self, token, version="v25.0", session=None, sleep=time.sleep):
        import requests
        self.token, self.v, self.s, self.sleep = token, version, session or requests.Session(), sleep

    def _check(self, r):
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400 or "error" in data:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise MetaError(f"HTTP {r.status_code}: {err.get('message') or r.text[:200]}")
        return data

    def post(self, path, **params):
        return self._check(self.s.post(f"{GRAPH}/{self.v}/{path}", data={**params, "access_token": self.token},
                                       timeout=120))

    def get(self, path, **params):
        return self._check(self.s.get(f"{GRAPH}/{self.v}/{path}", params={**params, "access_token": self.token},
                                      timeout=60))

    def upload(self, url, data):
        h = {"Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(len(data))}
        return self._check(self.s.post(url, headers=h, data=data, timeout=600))

    def instagram(self, ig_user_id, video, caption, wait_s=600):
        """Reel via resumable upload. Returns (media_id, permalink)."""
        c = self.post(f"{ig_user_id}/media", media_type="REELS", upload_type="resumable",
                      caption=caption, share_to_feed="true")
        cid = c["id"]
        self.upload(f"{RUPLOAD}/ig-api-upload/{self.v}/{cid}", video)
        waited = 0
        while True:
            st = self.get(cid, fields="status_code,status").get("status_code")
            if st == "FINISHED":
                break
            if st in ("ERROR", "EXPIRED"):
                raise MetaError(f"Instagram processing {st}")
            if waited >= wait_s:
                raise MetaError("Instagram still processing after 10 min")
            self.sleep(15)
            waited += 15
        mid = self.post(f"{ig_user_id}/media_publish", creation_id=cid)["id"]
        try:  # published already: a missing link must not cause a second post
            link = self.get(mid, fields="permalink").get("permalink", "")
        except MetaError:
            link = ""
        return mid, link or f"https://www.instagram.com/ (media {mid})"

    def facebook(self, page_id, video, caption):
        """Page Reel. Returns (video_id, url)."""
        vid = self.post(f"{page_id}/video_reels", upload_phase="start")["video_id"]
        self.upload(f"{RUPLOAD}/video-upload/{self.v}/{vid}", video)
        self.post(f"{page_id}/video_reels", upload_phase="finish", video_id=vid,
                  video_state="PUBLISHED", description=caption)
        return vid, f"https://www.facebook.com/reel/{vid}"


# --- Google Drive (service account) ---------------------------------------------------------

def drive_session():
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.service_account import Credentials
    info = sheets.load_service_account_info()
    return AuthorizedSession(Credentials.from_service_account_info(info, scopes=sheets.SCOPES))


def drive_file(session, batch_id):
    """Bytes of <batch_id>.mp4 from the Drive folder shared with the service account."""
    q = f"name = '{batch_id}.mp4' and trashed = false"
    r = session.get("https://www.googleapis.com/drive/v3/files",
                    params={"q": q, "fields": "files(id,size,modifiedTime)", "orderBy": "modifiedTime desc",
                            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}, timeout=60)
    r.raise_for_status()
    files = r.json().get("files", [])
    if not files:
        raise MetaError(f"{batch_id}.mp4 not found in Drive (is the GTA6 Clipper folder shared with "
                        "the service account?)")
    r = session.get(f"https://www.googleapis.com/drive/v3/files/{files[0]['id']}",
                    params={"alt": "media", "supportsAllDrives": "true"}, timeout=300)
    r.raise_for_status()
    return r.content


# --- run -----------------------------------------------------------------------------------------

def post_one(meta, tok, row, platform, video):
    if platform == "instagram":
        mid, url = meta.instagram(tok["ig_user_id"], video, caption_of(row))
    else:
        mid, url = meta.facebook(tok["page_id"], video, caption_of(row))
    return mid, url


def main(argv=None):
    dry = "--dry-run" in (argv or sys.argv[1:])
    settings = load_settings()
    cfg = settings.get("social", {})
    now = datetime.now(timezone.utc)
    if not cfg.get("enabled", True):
        print("Social posting is off (settings.social.enabled).")
        return 0
    try:
        tokens = load_tokens()
    except MetaError as e:
        print(f"SOCIAL FAILED: {e}")
        return 1
    if not tokens:
        print(f"Social posting not set up yet (secret {ENV} empty or no Page matched a brand). Nothing posted.")
        return 0
    for b in settings["brands"]:   # names only, never tokens
        t = tokens.get(b["key"])
        print(f"  {b['name']}: " + ("not connected" if not t else
              f"Instagram {'yes' if t.get('ig_user_id') else 'NO (link the Instagram to the Page)'}, "
              f"Facebook {'yes' if t.get('page_id') else 'no'}"))
    try:
        ss = sheets.open_sheet(sheets.get_client(), settings["sheet"]["id"])
        sheets.ensure_tab(ss, "Batch", TABS["Batch"])
        b_ws, p_ws = ss.worksheet("Batch"), ss.worksheet("Posted")
        todo = due(sheets.read_records(b_ws), tokens, cfg, now)
    except sheets.SheetSetupError as e:
        print(f"SOCIAL FAILED (sheet): {e}")
        return 1
    print(f"{len(todo)} post(s) due (veto window {cfg.get('veto_hours', 2)} h).")
    if dry or not todo:
        for r, p in todo:
            print(f"  would post {r['batch_id']} ({r['brand']}) to {p}")
        return 0
    drive, videos, batch_upd, posted = drive_session(), {}, {}, []
    for row, platform in todo:
        bid, tok = str(row["batch_id"]), tokens[row["brand"]]
        col = "ig" if platform == "instagram" else "fb"
        try:
            if bid not in videos:
                videos[bid] = drive_file(drive, bid)
            mid, url = post_one(Meta(tok["token"], cfg.get("graph_version", "v25.0")), tok, row, platform,
                                videos[bid])
        except Exception as e:  # one failure never stops the others
            msg = str(e).replace(tok["token"], "***")[:150]
            print(f"  {bid} {platform}: FAILED {msg}")
            batch_upd.setdefault(bid, {})[f"{col}_status"] = f"failed: {msg}"[:100]
            continue
        print(f"  {bid} {platform}: posted {url}")
        batch_upd.setdefault(bid, {}).update({f"{col}_status": "done", f"{col}_url": url, "social_at": iso(now)})
        posted.append({"post_id": f"{bid}-{platform}", "batch_id": bid, "moment_id": row.get("moment_id", ""),
                       "brand": row["brand"], "platform": platform, "url": url, "posted_at": iso(now),
                       "platform_id": mid})
    if batch_upd:
        sheets.set_values(b_ws, TABS["Batch"], "batch_id", batch_upd)
    if posted:
        sheets.upsert_rows(p_ws, TABS["Posted"], "post_id", posted, preserve=("posted_at",))
    print(f"Posted {len(posted)}, failed {len(todo) - len(posted)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
