"""
Adam_X — Find the first tweet on any topic, or the first tweet of any account.
APIs: GetXAPI + Twitter293 (RapidAPI)
Strategy: binary search in time → progressive zoom → exhaust micro-window
"""
import os, json, math, time, re, uuid, logging, threading
import urllib.parse as _up
import requests as _requests
from datetime import datetime, timezone, timedelta
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, request, jsonify, session, render_template_string
from flask_compress import Compress

# ── App setup ─────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger('adam_x')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(32))
Compress(app)

DATA_DIR   = 'data'
USERS_DIR  = os.path.join(DATA_DIR, 'users')
os.makedirs(USERS_DIR, exist_ok=True)

# ── Twitter Snowflake helpers ──────────────────────────────────────────────────
_TW_EPOCH_MS = 1288834974657  # 2010-11-04T01:42:54.657Z

def _dt_to_snowflake(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (int(dt.timestamp() * 1000) - _TW_EPOCH_MS) << 22)

def _snowflake_to_dt(sid) -> datetime:
    return datetime.fromtimestamp(((int(sid) >> 22) + _TW_EPOCH_MS) / 1000, tz=timezone.utc)

def _fmt_dt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime('%Y-%m-%d_%H:%M:%S_UTC')

def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S.%f%z',
                '%a %b %d %H:%M:%S %z %Y', '%Y-%m-%d_%H:%M:%S_UTC'):
        try:
            dt = datetime.strptime(str(raw).replace('Z', '+00:00'), fmt)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None

# ── Config & auth ──────────────────────────────────────────────────────────────
_ADMIN_FILE  = os.path.join(DATA_DIR, 'admin.json')
_CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')

def _load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}

def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def _get_admin():
    d = _load_json(_ADMIN_FILE, {})
    if not d.get('password_hash'):
        pw = os.environ.get('ADMIN_PASSWORD', 'adam1234')
        d = {'password_hash': generate_password_hash(pw)}
        _save_json(_ADMIN_FILE, d)
    return d

def _require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('authed'):
            return jsonify({'error': 'Non authentifié'}), 401
        return f(*args, **kwargs)
    return wrapper

def _load_cfg():
    cfg = _load_json(_CONFIG_FILE, {})
    cfg.setdefault('getxapi_key',  os.environ.get('GETXAPI_KEY', ''))
    cfg.setdefault('twitter293_key', os.environ.get('TWITTER293_KEY', ''))
    return cfg

# ── Jobs store ─────────────────────────────────────────────────────────────────
_jobs: dict = {}
_jobs_lock = threading.Lock()

def _job_set(jid, data):
    with _jobs_lock:
        _jobs[jid] = {**_jobs.get(jid, {}), **data}

# ── GetXAPI helpers ────────────────────────────────────────────────────────────
_gx_session = _requests.Session()
_gx_session.verify = False

def _getxapi_search(query: str, key: str, count: int = 5, cursor: str = None) -> dict:
    params = {'q': query, 'product': 'latest', 'count': count}
    if cursor:
        params['cursor'] = cursor
    r = _gx_session.get(
        'https://api.getxapi.com/twitter/tweet/advanced_search',
        headers={'Authorization': f'Bearer {key}'},
        params=params,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()

def _gx_parse_tweets(raw: dict) -> list:
    tweets = raw.get('tweets') or raw.get('data') or []
    if isinstance(tweets, dict):
        tweets = list(tweets.values())
    return tweets if isinstance(tweets, list) else []

def _gx_get_cursor(raw: dict) -> str | None:
    return raw.get('next_cursor') or raw.get('cursor') or None

def _gx_tweet_dt(t: dict) -> datetime | None:
    return _parse_dt(t.get('createdAt') or t.get('created_at'))

def _gx_normalize(t: dict) -> dict:
    author = t.get('author') or {}
    tid    = str(t.get('id') or t.get('rest_id') or '')
    dt     = _gx_tweet_dt(t)
    username = (author.get('userName') or author.get('screen_name') or
                author.get('username') or '').lower().strip('@')
    url = (t.get('twitterUrl') or t.get('url') or
           (f'https://x.com/{username}/status/{tid}' if tid and username else ''))
    return {
        'id':         tid,
        'text':       (t.get('text') or t.get('full_text') or '').strip(),
        'author':     username,
        'author_name': author.get('name') or author.get('displayName') or username,
        'created_at': dt.isoformat() if dt else '',
        'url':        url,
        'likes':      t.get('likeCount') or t.get('favorite_count') or 0,
        'retweets':   t.get('retweetCount') or t.get('retweet_count') or 0,
        'source':     'getxapi',
    }

# ── Twitter293 helpers ─────────────────────────────────────────────────────────
def _tw293_search(query: str, key: str, cursor: str = None,
                   since: datetime = None, until: datetime = None) -> dict:
    q_enc   = _up.quote(query, safe='')
    qs      = ['count=20', 'category=Latest']
    filters = {}
    if since:
        filters['since'] = _fmt_dt(since)
    if until:
        filters['until'] = _fmt_dt(until)
    if filters:
        qs.append('filters=' + _up.quote(json.dumps(filters, separators=(',', ':')), safe=''))
    if cursor:
        qs.append('cursor=' + _up.quote(cursor, safe=''))
    url = f'https://twitter293.p.rapidapi.com/search/{q_enc}?' + '&'.join(qs)
    r   = _requests.get(url, headers={
        'x-rapidapi-key':  key,
        'x-rapidapi-host': 'twitter293.p.rapidapi.com',
    }, timeout=(8, 20))
    r.raise_for_status()
    return r.json()

def _tw293_parse_tweets(raw: dict) -> list:
    """Parse Twitter293 response — recursive walker capturing full GraphQL result nodes."""
    if isinstance(raw.get('tweets'), list) and raw['tweets']:
        return raw['tweets']
    if isinstance(raw.get('result'), list) and raw['result']:
        return raw['result']
    if isinstance(raw, list) and raw:
        return raw

    found    = []
    seen_ids = set()

    def _walk(node, depth=0):
        if depth > 14 or not node:
            return
        if isinstance(node, list):
            for item in node:
                _walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return

        # Priority: capture the GraphQL result node (has legacy + core.user_results)
        tr = (node.get('tweet_results') or {}).get('result')
        if isinstance(tr, dict):
            leg = tr.get('legacy') or {}
            tid = str(tr.get('rest_id') or leg.get('id_str') or '')
            if tid and (leg.get('full_text') or leg.get('text')) and leg.get('created_at'):
                if tid not in seen_ids:
                    seen_ids.add(tid)
                    found.append({'_legacy': leg, '_result': tr})
                return

        # Fallback: flat tweet object (id_str + text + created_at at top level)
        tid = str(node.get('id_str') or node.get('rest_id') or '')
        has_text = bool(node.get('full_text') or node.get('text'))
        has_date = bool(node.get('created_at'))
        if tid and has_text and has_date and tid not in seen_ids:
            seen_ids.add(tid)
            found.append(node)
            return

        for v in node.values():
            if isinstance(v, (dict, list)):
                _walk(v, depth + 1)

    _walk(raw)
    return found

def _tw293_get_cursor(raw: dict) -> str | None:
    for k in ('next_cursor', 'cursor', 'nextCursor', 'bottom_cursor'):
        v = raw.get(k)
        if v and isinstance(v, str) and len(v) > 4:
            return v
    # GraphQL cursor entries
    try:
        instructions = (raw['data']['search_by_raw_query']['search_timeline']
                        ['timeline']['instructions'])
        for instr in instructions:
            for entry in (instr.get('entries') or []):
                eid = entry.get('entryId', '')
                c   = entry.get('content') or {}
                if 'cursor' in eid.lower() and c.get('cursorType', '').lower() == 'bottom':
                    return c.get('value') or c.get('cursor')
    except Exception:
        pass
    return None

def _tw293_tweet_dt(t: dict) -> datetime | None:
    if '_legacy' in t:
        return _parse_dt(t['_legacy'].get('created_at'))
    return _parse_dt(t.get('created_at') or t.get('createdAt'))

def _tw293_normalize(t: dict) -> dict:
    """Normalize any tweet-shaped object — works on legacy, GraphQL result, or flat tweet."""
    # Unwrap _legacy wrapper (old path)
    if '_legacy' in t:
        leg = t['_legacy']
        res = t.get('_result') or {}
        core = res.get('core') or {}
        udata = ((core.get('user_results') or {}).get('result') or {}).get('legacy') or {}
        tid  = str(leg.get('id_str') or res.get('rest_id') or '')
        username = udata.get('screen_name') or ''
        dt = _parse_dt(leg.get('created_at'))
        return {
            'id':          tid,
            'text':        (leg.get('full_text') or leg.get('text') or '').strip(),
            'author':      username.lower(),
            'author_name': udata.get('name') or username,
            'created_at':  dt.isoformat() if dt else '',
            'url':         (f'https://x.com/{username}/status/{tid}' if tid and username
                       else f'https://x.com/i/web/status/{tid}' if tid else ''),
            'likes':       leg.get('favorite_count', 0),
            'retweets':    leg.get('retweet_count', 0),
            'source':      'twitter293',
        }

    # Walker-extracted flat tweet (has id_str + text + created_at directly)
    tid      = str(t.get('id_str') or t.get('rest_id') or t.get('id') or '')
    text     = (t.get('full_text') or t.get('text') or '').strip()
    dt       = _parse_dt(t.get('created_at') or t.get('createdAt'))
    user     = t.get('user') or {}
    username = (user.get('screen_name') or t.get('username') or
                t.get('screen_name') or '').lower().strip('@')
    return {
        'id':          tid,
        'text':        text,
        'author':      username,
        'author_name': user.get('name') or t.get('name') or username,
        'created_at':  dt.isoformat() if dt else '',
        'url':         (f'https://x.com/{username}/status/{tid}' if tid and username
                       else f'https://x.com/i/web/status/{tid}' if tid else ''),
        'likes':       t.get('favorite_count', 0) or t.get('likeCount', 0),
        'retweets':    t.get('retweet_count', 0) or t.get('retweetCount', 0),
        'source':      'twitter293',
    }

# ── Core algorithm ─────────────────────────────────────────────────────────────
_TWITTER_BIRTH = datetime(2006, 3, 21, tzinfo=timezone.utc)  # first tweet ever

def _log_step(jid: str, msg: str):
    logger.info(f'[{jid}] {msg}')
    with _jobs_lock:
        log = _jobs.get(jid, {}).get('log', [])
        log.append({'ts': _fmt_dt(datetime.now(timezone.utc)), 'msg': msg})
        _jobs[jid]['log'] = log[-50:]  # keep last 50 lines


def _gx_has_results(query: str, key: str, since: datetime, until: datetime,
                    jid: str = None) -> bool:
    q = f'{query} since:{_fmt_dt(since)} until:{_fmt_dt(until)}'
    try:
        raw    = _getxapi_search(q, key, count=1)
        tweets = _gx_parse_tweets(raw)
        if jid and not tweets:
            top_keys = list(raw.keys()) if isinstance(raw, dict) else type(raw).__name__
            sample   = str(raw)[:200]
            _log_step(jid, f'  GX probe 0 results — keys:{top_keys} sample:{sample}')
        return len(tweets) > 0
    except Exception as e:
        if jid:
            _log_step(jid, f'  GX probe error: {e}')
        logger.warning(f'GetXAPI probe error: {e}')
        return False


def _tw293_has_results(query: str, key: str, since: datetime, until: datetime) -> bool:
    """Returns True if Twitter293 finds at least one tweet in [since, until]."""
    q = f'{query} since:{_fmt_dt(since)} until:{_fmt_dt(until)}'
    try:
        raw = _tw293_search(q, key)
        return len(_tw293_parse_tweets(raw)) > 0
    except Exception as e:
        logger.warning(f'TW293 probe error: {e}')
        return False


def _binary_search_epoch(query: str, gx_key: str, tw_key: str, jid: str,
                          low: datetime, high: datetime,
                          max_iters: int = 16) -> tuple[datetime, datetime] | None:
    """
    Binary search: find the earliest time slice that contains tweets about `query`.
    Returns (window_start, window_end) of the earliest 6-month-ish window with results,
    or None if nothing found at all.
    """
    # Use whichever API is available — GetXAPI preferred (faster probes)
    def _has(since, until):
        if gx_key:
            return _gx_has_results(query, gx_key, since, until, jid=jid)
        return _tw293_has_results(query, tw_key, since, until)

    _log_step(jid, f'Binary search ({"GX" if gx_key else "TW293"}): [{_fmt_dt(low)} → {_fmt_dt(high)}]')

    # Quick sanity: does anything exist at all?
    if not (gx_key and _gx_has_results(query, gx_key, low, high, jid=jid)
            or tw_key and _tw293_has_results(query, tw_key, low, high)):
        _log_step(jid, '  No results in full range — query returns nothing')
        return None

    earliest_window = (low, high)

    for i in range(max_iters):
        mid = low + (high - low) / 2
        span_days = (high - low).days
        _log_step(jid, f'  iter {i+1}: probe [{_fmt_dt(low)} → {_fmt_dt(mid)}] ({span_days}j)')

        if span_days < 1:
            earliest_window = (low, high)
            break

        if _has(low, mid):
            earliest_window = (low, mid)
            high = mid
        else:
            low = mid

    return earliest_window


def _zoom_to_micro_window(query: str, key: str, jid: str,
                           win_start: datetime, win_end: datetime,
                           target_page_size: int = 20) -> tuple[datetime, datetime]:
    """
    Progressive zoom: halve the window until it's small enough that a single API
    page can exhaust it (≤target_page_size tweets).
    Returns the smallest window still containing tweets.
    """
    _log_step(jid, f'Zoom: [{_fmt_dt(win_start)} → {_fmt_dt(win_end)}]')
    low, high = win_start, win_end
    MIN_WINDOW_MINUTES = 15

    while True:
        span_min = (high - low).total_seconds() / 60
        if span_min <= MIN_WINDOW_MINUTES:
            _log_step(jid, f'  micro-window atteinte: {span_min:.0f} min')
            break

        # Count tweets in first half
        mid = low + (high - low) / 2
        try:
            q_slice = f'{query} since:{_fmt_dt(low)} until:{_fmt_dt(mid)}'
            data    = _getxapi_search(q_slice, key, count=target_page_size + 1)
            tweets  = _gx_parse_tweets(data)
            count   = len(tweets)
        except Exception as e:
            _log_step(jid, f'  GetXAPI error during zoom: {e}')
            break

        _log_step(jid, f'  zoom probe [{_fmt_dt(low)} → {_fmt_dt(mid)}]: {count} tweets')

        if count == 0:
            # Nothing in first half → oldest tweet is in second half [low, high] stays but push low up
            low = mid
        else:
            # Tweets exist in first half → zoom into it to find the oldest
            high = mid

    # Safety: always return a window that spans at least 2h to avoid empty exhaust
    if (high - low).total_seconds() < 7200:
        low = low - timedelta(hours=1)
        high = high + timedelta(hours=1)

    return low, high


def _tw293_exhaust(q_with_ops: str, key: str, jid: str, max_pages: int = 30) -> list:
    """Paginate Twitter293 exhaustively on a query that already contains since:/until: operators."""
    seen  = set()
    all_t = []
    cursor = None

    for page in range(max_pages):
        try:
            raw    = _tw293_search(q_with_ops, key, cursor=cursor)
        except Exception as e:
            _log_step(jid, f'  TW293 HTTP error page {page+1}: {e}')
            break

        # Debug: log raw response structure on first page
        if page == 0:
            top_keys = list(raw.keys()) if isinstance(raw, dict) else f'type={type(raw).__name__}'
            sample   = str(raw)[:300]
            _log_step(jid, f'  TW293 raw keys: {top_keys}')
            _log_step(jid, f'  TW293 raw sample: {sample}')

        tweets = _tw293_parse_tweets(raw)
        cursor = _tw293_get_cursor(raw)

        new = 0
        for t in tweets:
            norm = _tw293_normalize(t)
            tid  = norm['id'] or norm['text'][:40]
            if tid in seen:
                continue
            seen.add(tid)
            if norm['created_at']:
                all_t.append(norm)
            new += 1

        _log_step(jid, f'  TW293 page {page+1}: +{new} (total={len(all_t)})')

        if not cursor or not tweets or new == 0:
            break

    return all_t


def _exhaust_window_tw293(query: str, key: str, jid: str,
                           win_start: datetime, win_end: datetime,
                           max_pages: int = 30) -> list:
    """Exhaust micro-window via Twitter293 — operators embedded in query string."""
    # Strategy 1: since:/until: operators in the query (most reliable)
    q_ops = f'{query} since:{_fmt_dt(win_start)} until:{_fmt_dt(win_end)}'
    _log_step(jid, f'TW293 exhaust (ops): {q_ops}')
    results = _tw293_exhaust(q_ops, key, jid, max_pages)
    if results:
        return results

    # Strategy 2: just since: operator (until: sometimes causes empty results)
    q_since = f'{query} since:{_fmt_dt(win_start)}'
    _log_step(jid, f'TW293 exhaust (since only): {q_since}')
    results = _tw293_exhaust(q_since, key, jid, max_pages=5)
    if results:
        # Filter locally to the window
        cutoff = win_end + timedelta(hours=2)
        results = [t for t in results
                   if _parse_dt(t['created_at']) and _parse_dt(t['created_at']) <= cutoff]
        if results:
            return results

    # Strategy 3: plain query, no time constraint — take oldest from first pages
    _log_step(jid, f'TW293 exhaust (plain fallback): {query}')
    results = _tw293_exhaust(query, key, jid, max_pages=3)
    return results


def _exhaust_window_gx(query: str, key: str, jid: str,
                        win_start: datetime, win_end: datetime,
                        max_pages: int = 20) -> list:
    """Fallback: exhaust micro-window via GetXAPI if Twitter293 yields nothing."""
    _log_step(jid, f'GX exhaust fallback [{_fmt_dt(win_start)} → {_fmt_dt(win_end)}]')
    q      = f'{query} since:{_fmt_dt(win_start)} until:{_fmt_dt(win_end)}'
    seen   = set()
    all_t  = []
    cursor = None

    for page in range(max_pages):
        try:
            raw    = _getxapi_search(q, key, count=50, cursor=cursor)
            tweets = _gx_parse_tweets(raw)
            cursor = _gx_get_cursor(raw)
        except Exception as e:
            _log_step(jid, f'  GX fallback error page {page}: {e}')
            break

        new = 0
        for t in tweets:
            norm = _gx_normalize(t)
            tid  = norm['id'] or norm['text'][:40]
            if tid in seen:
                continue
            seen.add(tid)
            if norm['created_at']:
                all_t.append(norm)
            new += 1

        _log_step(jid, f'  GX page {page+1}: +{new}')
        if not cursor or not tweets or new == 0:
            break

    return all_t


def _find_first_tweet(query: str, cfg: dict, jid: str,
                       since_dt: datetime = None) -> dict:
    """
    Main algorithm:
      1. Binary search (GetXAPI) → earliest epoch window
      2. Progressive zoom (GetXAPI) → micro-window ≤15min
      3. Exhaust micro-window (Twitter293 primary, GetXAPI fallback)
      4. Return the oldest tweet found
    """
    gx_key  = cfg.get('getxapi_key', '')
    tw_key  = cfg.get('twitter293_key', '')

    if not gx_key and not tw_key:
        _job_set(jid, {'status': 'error', 'error': 'Aucune clé API configurée'})
        return {}

    low  = since_dt or _TWITTER_BIRTH
    high = datetime.now(timezone.utc)

    # ── Phase 1: binary search (GetXAPI preferred, TW293 fallback) ───────────────
    window = _binary_search_epoch(query, gx_key, tw_key, jid, low, high)
    if not window:
        _job_set(jid, {'status': 'done', 'result': None,
                       'msg': 'Aucun tweet trouvé pour cette requête.'})
        return {}
    win_start, win_end = window

    _job_set(jid, {'phase': 'zoom', 'window': f'{_fmt_dt(win_start)} → {_fmt_dt(win_end)}'})

    # ── Phase 2: zoom to micro-window (GetXAPI only — TW293 too slow for probes) ─
    if gx_key:
        win_start, win_end = _zoom_to_micro_window(query, gx_key, jid, win_start, win_end)

    # Extend win_end slightly to not miss edge tweets
    win_end_padded = win_end + timedelta(hours=1)

    _job_set(jid, {'phase': 'exhaust', 'window': f'{_fmt_dt(win_start)} → {_fmt_dt(win_end)}'})

    # ── Phase 3: exhaust micro-window ──────────────────────────────────────────
    all_tweets = []
    if tw_key:
        all_tweets = _exhaust_window_tw293(query, tw_key, jid, win_start, win_end_padded)
    if not all_tweets and gx_key:
        all_tweets = _exhaust_window_gx(query, gx_key, jid, win_start, win_end_padded)

    if not all_tweets:
        _job_set(jid, {'status': 'done', 'result': None,
                       'msg': 'Micro-window vide — sujet peut-être trop récent ou trop rare.'})
        return {}

    # ── Phase 4: pick oldest ───────────────────────────────────────────────────
    def _sort_key(t):
        dt = _parse_dt(t.get('created_at'))
        return dt if dt else datetime.max.replace(tzinfo=timezone.utc)

    all_tweets.sort(key=_sort_key)
    first = all_tweets[0]
    _log_step(jid, f'First tweet found: @{first["author"]} — {first["created_at"]}')
    _log_step(jid, f'  "{first["text"][:80]}"')

    return first


def _find_first_account_tweet(username: str, cfg: dict, jid: str) -> dict:
    """
    For account mode: use from:username + binary search.
    Much simpler — accounts rarely have >500k tweets, and the oldest is guaranteed
    to be in the earliest window.
    """
    clean = username.lower().strip('@')
    query = f'from:{clean}'
    return _find_first_tweet(query, cfg, jid, since_dt=_TWITTER_BIRTH)


# ── Background worker ──────────────────────────────────────────────────────────
def _run_job(jid: str, mode: str, query: str, username: str,
              cfg: dict, since_dt: datetime | None):
    try:
        _job_set(jid, {'status': 'running', 'phase': 'search', 'log': []})

        if mode == 'account':
            result = _find_first_account_tweet(username, cfg, jid)
        else:
            result = _find_first_tweet(query, cfg, jid, since_dt)

        if result:
            _job_set(jid, {'status': 'done', 'phase': 'done', 'result': result})
        else:
            _job_set(jid, {'status': 'done', 'phase': 'done', 'result': None})
    except Exception as e:
        logger.exception(f'Job {jid} crashed: {e}')
        _job_set(jid, {'status': 'error', 'error': str(e)})


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    if not session.get('authed'):
        return render_template_string(_LOGIN_HTML)
    return render_template_string(_APP_HTML)

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    body = request.get_json(silent=True) or {}
    pw   = body.get('password', '')
    adm  = _get_admin()
    if check_password_hash(adm['password_hash'], pw):
        session['authed'] = True
        return jsonify({'ok': True})
    return jsonify({'error': 'Mot de passe incorrect'}), 401

@app.route('/api/auth/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'ok': True})

@app.route('/api/config', methods=['GET', 'POST'])
@_require_auth
def api_config():
    cfg = _load_cfg()
    if request.method == 'GET':
        return jsonify({k: ('***' if v else '') for k, v in cfg.items()})
    body = request.get_json(silent=True) or {}
    for k in ('getxapi_key', 'twitter293_key'):
        if k in body:
            cfg[k] = str(body[k]).strip()
    _save_json(_CONFIG_FILE, cfg)
    return jsonify({'ok': True})

@app.route('/api/adam/search', methods=['POST'])
@_require_auth
def api_search():
    body     = request.get_json(silent=True) or {}
    mode     = body.get('mode', 'topic')   # 'topic' | 'account'
    query    = str(body.get('query', '')).strip()
    username = str(body.get('username', '')).strip().lstrip('@')
    since_s  = str(body.get('since', '')).strip()

    if mode == 'account' and not username:
        return jsonify({'error': 'username requis pour le mode account'}), 400
    if mode == 'topic' and not query:
        return jsonify({'error': 'query requise pour le mode topic'}), 400

    since_dt = None
    if since_s:
        try:
            since_dt = datetime.fromisoformat(since_s).replace(tzinfo=timezone.utc)
        except Exception:
            pass

    cfg = _load_cfg()
    if not cfg.get('getxapi_key') and not cfg.get('twitter293_key'):
        return jsonify({'error': 'Aucune clé API configurée — va dans Paramètres'}), 400

    jid = uuid.uuid4().hex[:12]
    _job_set(jid, {'status': 'queued', 'mode': mode, 'query': query,
                   'username': username, 'ts': time.time()})

    t = threading.Thread(
        target=_run_job,
        args=(jid, mode, query, username, cfg, since_dt),
        daemon=True,
    )
    t.start()
    return jsonify({'job_id': jid})

@app.route('/api/adam/status/<jid>', methods=['GET'])
@_require_auth
def api_status(jid: str):
    with _jobs_lock:
        job = _jobs.get(jid)
    if not job:
        return jsonify({'error': 'Job introuvable'}), 404
    return jsonify(job)

@app.route('/api/adam/jobs', methods=['GET'])
@_require_auth
def api_jobs():
    with _jobs_lock:
        items = [{'id': k, **{kk: vv for kk, vv in v.items() if kk != 'log'}}
                 for k, v in _jobs.items()]
    items.sort(key=lambda x: x.get('ts', 0), reverse=True)
    return jsonify(items[:50])

# ── HTML templates ─────────────────────────────────────────────────────────────
_LOGIN_HTML = '''<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Adam_X — Login</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0f;color:#e2e8f0;font-family:'Inter',system-ui,sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh}
.card{background:#12121a;border:1px solid #1e1e2e;border-radius:12px;padding:40px;width:340px}
h1{font-size:22px;font-weight:700;margin-bottom:6px;color:#fff}
.sub{color:#64748b;font-size:13px;margin-bottom:28px}
label{display:block;font-size:12px;color:#94a3b8;margin-bottom:6px}
input{width:100%;padding:10px 14px;background:#1e1e2e;border:1px solid #2d2d3d;
      border-radius:8px;color:#e2e8f0;font-size:14px;outline:none}
input:focus{border-color:#6366f1}
button{margin-top:16px;width:100%;padding:11px;background:#6366f1;border:none;
       border-radius:8px;color:#fff;font-size:14px;font-weight:600;cursor:pointer}
button:hover{background:#4f46e5}
.err{color:#f87171;font-size:12px;margin-top:10px}
</style>
</head>
<body>
<div class="card">
  <h1>Adam_X</h1>
  <p class="sub">First tweet finder</p>
  <label>Mot de passe</label>
  <input type="password" id="pw" placeholder="••••••••" onkeydown="if(event.key==='Enter')login()">
  <button onclick="login()">Connexion</button>
  <p class="err" id="err"></p>
</div>
<script>
async function login(){
  const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password:document.getElementById('pw').value})});
  if(r.ok)location.reload();
  else document.getElementById('err').textContent='Mot de passe incorrect';
}
</script>
</body></html>'''

_APP_HTML = '''<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Adam_X — First Tweet Finder</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0a0a0f;color:#e2e8f0;font-family:'Inter',system-ui,sans-serif;min-height:100vh}
.header{padding:16px 24px;border-bottom:1px solid #1e1e2e;display:flex;align-items:center;
        justify-content:space-between;background:#0d0d14}
.logo{font-size:18px;font-weight:700;color:#fff}
.logo span{color:#6366f1}
.nav-right{display:flex;gap:10px;align-items:center}
.btn-sm{padding:6px 14px;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;border:none}
.btn-ghost{background:transparent;color:#64748b;border:1px solid #2d2d3d}
.btn-ghost:hover{color:#e2e8f0;border-color:#6366f1}
.btn-primary{background:#6366f1;color:#fff}
.btn-primary:hover{background:#4f46e5}
.main{max-width:780px;margin:48px auto;padding:0 24px}
h2{font-size:28px;font-weight:700;margin-bottom:6px}
.desc{color:#64748b;font-size:14px;margin-bottom:36px}
.card{background:#12121a;border:1px solid #1e1e2e;border-radius:12px;padding:24px;margin-bottom:20px}
.tabs{display:flex;gap:0;margin-bottom:24px;background:#0a0a0f;border-radius:9px;padding:3px;
      border:1px solid #1e1e2e;width:fit-content}
.tab{padding:8px 20px;border-radius:7px;font-size:13px;font-weight:600;cursor:pointer;
     color:#64748b;transition:all .15s}
.tab.active{background:#6366f1;color:#fff}
label{display:block;font-size:12px;color:#94a3b8;margin-bottom:6px;margin-top:16px}
label:first-of-type{margin-top:0}
input,select{width:100%;padding:10px 14px;background:#1e1e2e;border:1px solid #2d2d3d;
      border-radius:8px;color:#e2e8f0;font-size:14px;outline:none}
input:focus,select:focus{border-color:#6366f1}
.row{display:flex;gap:12px}
.row>div{flex:1}
.go-btn{margin-top:20px;width:100%;padding:13px;background:#6366f1;border:none;
        border-radius:9px;color:#fff;font-size:15px;font-weight:700;cursor:pointer;
        transition:background .15s}
.go-btn:hover:not(:disabled){background:#4f46e5}
.go-btn:disabled{opacity:.5;cursor:not-allowed}
.status-bar{margin-top:20px;padding:12px 16px;background:#0d0d14;border-radius:9px;
            border:1px solid #1e1e2e;font-size:13px;color:#94a3b8;display:none}
.status-bar.visible{display:block}
.phase-dot{display:inline-block;width:8px;height:8px;border-radius:50%;
           background:#6366f1;margin-right:8px;animation:pulse 1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.log-box{margin-top:10px;max-height:160px;overflow-y:auto;font-size:11px;color:#475569;
         font-family:monospace;line-height:1.6}
/* Result card */
.result-card{background:#0d1f0d;border:1px solid #16a34a;border-radius:12px;padding:20px;
             margin-top:20px;display:none}
.result-card.visible{display:block}
.result-badge{font-size:11px;font-weight:700;color:#4ade80;letter-spacing:.05em;margin-bottom:12px}
.result-text{font-size:15px;color:#e2e8f0;line-height:1.6;margin-bottom:14px;
             background:#0a0a0f;padding:14px;border-radius:8px}
.result-meta{display:flex;flex-wrap:wrap;gap:10px}
.meta-item{background:#1e1e2e;padding:5px 12px;border-radius:6px;font-size:12px;color:#94a3b8}
.meta-item b{color:#e2e8f0}
.result-link{display:inline-flex;align-items:center;gap:6px;margin-top:14px;padding:8px 16px;
             background:#1d4ed8;border-radius:8px;color:#fff;font-size:13px;font-weight:600;
             text-decoration:none}
.result-link:hover{background:#1e40af}
.no-result{color:#f87171;font-size:13px;margin-top:12px}
/* Config panel */
.config-panel{display:none;margin-top:20px}
.config-panel.visible{display:block}
.save-btn{margin-top:12px;padding:9px 20px;background:#1e1e2e;border:1px solid #2d2d3d;
          border-radius:8px;color:#e2e8f0;font-size:13px;font-weight:600;cursor:pointer}
.save-btn:hover{background:#2d2d3d}
.ok-msg{color:#4ade80;font-size:12px;margin-left:10px;display:none}
.history{margin-top:8px}
.hist-item{padding:10px 14px;background:#0d0d14;border-radius:8px;border:1px solid #1e1e2e;
           margin-bottom:8px;cursor:pointer;font-size:13px}
.hist-item:hover{border-color:#6366f1}
.hist-item .hi-query{color:#e2e8f0;font-weight:600}
.hist-item .hi-date{color:#4ade80;font-size:11px;margin-top:3px}
.hist-item .hi-text{color:#64748b;font-size:11px;margin-top:2px;white-space:nowrap;
                    overflow:hidden;text-overflow:ellipsis}
</style>
</head>
<body>
<div class="header">
  <div class="logo">Adam<span>_X</span></div>
  <div class="nav-right">
    <button class="btn-sm btn-ghost" onclick="toggleConfig()">⚙ API Keys</button>
    <button class="btn-sm btn-ghost" onclick="logout()">Déconnexion</button>
  </div>
</div>

<div class="main">
  <h2>First Tweet Finder</h2>
  <p class="desc">Retrouve le tout premier tweet sur un sujet, ou le premier tweet d'un compte.</p>

  <!-- Config panel -->
  <div class="config-panel" id="configPanel">
    <div class="card">
      <div style="font-size:14px;font-weight:600;margin-bottom:16px;color:#94a3b8">API Keys</div>
      <label>GetXAPI Key</label>
      <input type="password" id="cfg_gx" placeholder="Bearer token GetXAPI">
      <label>Twitter293 Key (RapidAPI)</label>
      <input type="password" id="cfg_tw293" placeholder="RapidAPI key">
      <button class="save-btn" onclick="saveConfig()">Sauvegarder</button>
      <span class="ok-msg" id="cfgOk">✓ Sauvegardé</span>
    </div>
  </div>

  <!-- Search card -->
  <div class="card">
    <div class="tabs">
      <div class="tab active" id="tab-topic" onclick="setMode('topic')">Sujet / Mot-clé</div>
      <div class="tab" id="tab-account" onclick="setMode('account')">Compte @</div>
    </div>

    <!-- Topic mode -->
    <div id="form-topic">
      <label>Requête de recherche</label>
      <input type="text" id="q-topic" placeholder='ex: "fake news" OR desinformation lang:fr'>
      <label>Chercher depuis (optionnel)</label>
      <input type="date" id="since-date" value="2006-03-21">
    </div>

    <!-- Account mode -->
    <div id="form-account" style="display:none">
      <label>Nom d'utilisateur</label>
      <input type="text" id="q-account" placeholder="elonmusk (sans @)">
    </div>

    <button class="go-btn" id="goBtn" onclick="startSearch()">Trouver le premier tweet →</button>

    <div class="status-bar" id="statusBar">
      <span class="phase-dot"></span>
      <span id="statusText">Recherche en cours...</span>
      <div class="log-box" id="logBox"></div>
    </div>

    <div class="result-card" id="resultCard">
      <div class="result-badge">🥇 PREMIER TWEET TROUVÉ</div>
      <div class="result-text" id="resultText"></div>
      <div class="result-meta" id="resultMeta"></div>
      <a id="resultLink" class="result-link" href="#" target="_blank">Voir sur X/Twitter ↗</a>
    </div>
    <p class="no-result" id="noResult" style="display:none"></p>
  </div>

  <!-- History -->
  <div class="card" id="histCard" style="display:none">
    <div style="font-size:13px;font-weight:600;color:#64748b;margin-bottom:12px">Recherches récentes</div>
    <div class="history" id="histList"></div>
  </div>
</div>

<script>
let mode = 'topic';
let pollTimer = null;

function setMode(m) {
  mode = m;
  document.getElementById('tab-topic').classList.toggle('active', m === 'topic');
  document.getElementById('tab-account').classList.toggle('active', m === 'account');
  document.getElementById('form-topic').style.display   = m === 'topic'   ? '' : 'none';
  document.getElementById('form-account').style.display = m === 'account' ? '' : 'none';
}

function toggleConfig() {
  const p = document.getElementById('configPanel');
  p.classList.toggle('visible');
  if (p.classList.contains('visible')) loadConfigKeys();
}

async function loadConfigKeys() {
  const r = await fetch('/api/config');
  if (!r.ok) return;
  const d = await r.json();
  document.getElementById('cfg_gx').placeholder    = d.getxapi_key    ? '(configuré)' : 'Bearer token GetXAPI';
  document.getElementById('cfg_tw293').placeholder = d.twitter293_key ? '(configuré)' : 'RapidAPI key';
}

async function saveConfig() {
  const body = {};
  const gx   = document.getElementById('cfg_gx').value.trim();
  const tw   = document.getElementById('cfg_tw293').value.trim();
  if (gx)  body.getxapi_key    = gx;
  if (tw)  body.twitter293_key = tw;
  await fetch('/api/config', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const ok = document.getElementById('cfgOk');
  ok.style.display = 'inline';
  setTimeout(() => ok.style.display = 'none', 2000);
}

async function startSearch() {
  clearResult();
  const btn = document.getElementById('goBtn');
  btn.disabled = true;

  const body = {mode};
  if (mode === 'topic') {
    body.query = document.getElementById('q-topic').value.trim();
    body.since = document.getElementById('since-date').value;
  } else {
    body.username = document.getElementById('q-account').value.trim().replace('@','');
  }

  showStatus('Lancement...');
  const r = await fetch('/api/adam/search', {method:'POST',
    headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  const d = await r.json();
  if (!r.ok) { showError(d.error || 'Erreur'); btn.disabled = false; return; }

  pollJob(d.job_id);
}

function pollJob(jid) {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    const r = await fetch(`/api/adam/status/${jid}`);
    const d = await r.json();

    const phase = d.phase || d.status;
    const phaseLabels = {
      'queued':'En queue...', 'search':'Recherche binaire...', 'zoom':'Zoom sur la fenêtre...',
      'exhaust':'Exhaustion micro-fenêtre...', 'done':'Terminé', 'error':'Erreur'
    };
    showStatus(phaseLabels[phase] || phase, d.log || []);

    if (d.status === 'done') {
      document.getElementById('goBtn').disabled = false;
      if (d.result) {
        showResult(d.result);
        saveToHistory(d);
        loadHistory();
      } else {
        showError(d.msg || 'Aucun tweet trouvé.');
      }
    } else if (d.status === 'error') {
      document.getElementById('goBtn').disabled = false;
      showError(d.error || 'Erreur inconnue');
    } else {
      pollJob(jid);
    }
  }, 1500);
}

function showStatus(msg, log) {
  const bar = document.getElementById('statusBar');
  bar.classList.add('visible');
  document.getElementById('statusText').textContent = msg;
  if (log && log.length) {
    document.getElementById('logBox').innerHTML = log.map(l =>
      `<div>${l.ts ? l.ts.replace('_UTC','') : ''} — ${l.msg}</div>`).join('');
    const lb = document.getElementById('logBox');
    lb.scrollTop = lb.scrollHeight;
  }
}

function showResult(r) {
  document.getElementById('statusBar').classList.remove('visible');
  const card = document.getElementById('resultCard');
  card.classList.add('visible');
  document.getElementById('resultText').textContent = r.text || '(texte non disponible)';
  const date = r.created_at ? new Date(r.created_at).toLocaleString('fr-FR',
    {year:'numeric',month:'long',day:'numeric',hour:'2-digit',minute:'2-digit',timeZone:'UTC'}) : '?';
  document.getElementById('resultMeta').innerHTML = `
    <div class="meta-item">✍ <b>@${r.author || '?'}</b></div>
    <div class="meta-item">📅 <b>${date}</b></div>
    <div class="meta-item">❤ <b>${r.likes ?? 0}</b></div>
    <div class="meta-item">🔁 <b>${r.retweets ?? 0}</b></div>
    <div class="meta-item">📡 <b>${r.source || '?'}</b></div>`;
  const link = document.getElementById('resultLink');
  link.href = r.url || '#';
  link.style.display = r.url ? '' : 'none';
}

function showError(msg) {
  // Keep status bar (log) visible so user can diagnose
  const p = document.getElementById('noResult');
  p.textContent = '✗ ' + msg;
  p.style.display = '';
  // Remove the pulse animation
  const dot = document.querySelector('.phase-dot');
  if (dot) dot.style.animation = 'none';
}

function clearResult() {
  document.getElementById('resultCard').classList.remove('visible');
  document.getElementById('noResult').style.display = 'none';
  document.getElementById('statusBar').classList.remove('visible');
  document.getElementById('logBox').innerHTML = '';
}

// ── Local history (localStorage) ──────────────────────────────────────────────
function saveToHistory(job) {
  if (!job.result) return;
  const hist = JSON.parse(localStorage.getItem('adam_x_hist') || '[]');
  hist.unshift({
    mode:  job.mode,
    query: job.query || job.username,
    result: job.result,
    ts: new Date().toISOString()
  });
  localStorage.setItem('adam_x_hist', JSON.stringify(hist.slice(0,20)));
}

function loadHistory() {
  const hist = JSON.parse(localStorage.getItem('adam_x_hist') || '[]');
  const list = document.getElementById('histList');
  const card = document.getElementById('histCard');
  if (!hist.length) { card.style.display = 'none'; return; }
  card.style.display = '';
  list.innerHTML = hist.map((h,i) => {
    const date = h.result?.created_at ? new Date(h.result.created_at).toLocaleDateString('fr-FR',
      {year:'numeric',month:'short',day:'numeric'}) : '';
    return `<div class="hist-item" onclick="replayResult(${i})">
      <div class="hi-query">${h.query || '?'}</div>
      <div class="hi-date">${date ? '📅 ' + date : ''} — @${h.result?.author || '?'}</div>
      <div class="hi-text">${h.result?.text?.slice(0,100) || ''}</div>
    </div>`;
  }).join('');
}

function replayResult(i) {
  const hist = JSON.parse(localStorage.getItem('adam_x_hist') || '[]');
  if (hist[i]) { clearResult(); showResult(hist[i].result); }
}

async function logout() {
  await fetch('/api/auth/logout',{method:'POST'});
  location.reload();
}

loadHistory();
</script>
</body></html>'''

# ── Entrypoint ─────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5010))
    logger.info(f'Adam_X starting on port {port}')
    app.run(host='0.0.0.0', port=port, debug=False)
