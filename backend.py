from dotenv import load_dotenv
load_dotenv()

from modules import botting
import os, sqlite3, secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import Flask, request, jsonify, session, redirect, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
import stripe
import re

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('BOLT_DB', os.path.join(APP_DIR, 'bolt.sqlite3'))
SECRET_FILE = os.path.join(APP_DIR, '.bolt_session_secret')

def load_secret():
    configured = os.environ.get('FLASK_SECRET_KEY')

    if configured:
        return configured

    try:
        with open(SECRET_FILE, 'r', encoding='utf-8') as f:
            value = f.read().strip()
            if value:
                return value
    except FileNotFoundError:
        pass

    value = secrets.token_hex(32)

    with open(SECRET_FILE, 'w', encoding='utf-8') as f:
        f.write(value)

    try:
        os.chmod(SECRET_FILE, 0o600)
    except OSError:
        pass

    return value

app = Flask(__name__, static_folder='static')
app.secret_key = load_secret()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=timedelta(days=14))
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', 'http://127.0.0.1:5000').rstrip('/')
ADMIN_USERNAMES = {x.strip().lower() for x in os.environ.get('ADMIN_USERNAMES', 'admin').split(',') if x.strip()}
PACKAGES = [
    {'id':'10','name':'Small Package','credits':10,'amount':50,'price_display':'A$0.50','featured':False},
    {'id':'50','name':'Medium Package','credits':50,'amount':80,'price_display':'A$0.80','featured':True},
    {'id':'100','name':'Big Package','credits':100,'amount':140,'price_display':'A$1.40','featured':False},
]

# Safety switch:
# True  = simulate bot actions
# False = use the real worker implementation
TESTING_MODE = False

if not TESTING_MODE:
    app.logger.warning(
        "TESTING MODE IS DISABLED. Real worker code may execute."
    )


COST_PER_BOT = 2
task_executor = ThreadPoolExecutor(max_workers=2)

def update_job_status(job_id, status):
    allowed = {"queued", "running", "completed", "failed"}
    if status not in allowed:
        raise ValueError("Invalid job status")

    with db() as con:
        con.execute(
            "UPDATE jobs SET status=? WHERE id=?",
            (status, job_id),
        )


def run_job(job_id):
    try:
        update_job_status(job_id, "running")

        with db() as con:
            job = con.execute(
                "SELECT * FROM jobs WHERE id = ?",
                (job_id,),
            ).fetchone()

        if job is None:
            raise ValueError(f"Job {job_id} not found")

        if TESTING_MODE:
            # Simulate a successful bot task without connecting any bots.
            app.logger.warning(
                "[TEST MODE] Simulating job=%s target=%s bots=%s",
                job_id,
                job["target"],
                job["bots"],
            )

        else:
            botting.engage_user(str(job["target"]), int(job["bots"]))

        update_job_status(job_id, "completed")

    except Exception:
        app.logger.exception("Job %s failed", job_id)

        try:
            update_job_status(job_id, "failed")
        except Exception:
            app.logger.exception(
                "Could not update failed status for job %s",
                job_id,
            )

def db():
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con

def init_db():
    with db() as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, credits INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, target TEXT NOT NULL, bots INTEGER NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
        CREATE TABLE IF NOT EXISTS payments(session_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, package_id TEXT NOT NULL, credited INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
        ''')

        con.execute("""
            CREATE TABLE IF NOT EXISTS credit_gifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_user_id INTEGER NOT NULL,
                recipient_user_id INTEGER NOT NULL,
                credits INTEGER NOT NULL CHECK (credits > 0),
                created_at TEXT NOT NULL,
                FOREIGN KEY (admin_user_id) REFERENCES users(id),
                FOREIGN KEY (recipient_user_id) REFERENCES users(id)
            )
        """)

        con.execute("""
            INSERT OR IGNORE INTO redeem_codes
                (code, credits, max_uses, uses, expires_at, enabled, created_at)
            VALUES (?, ?, ?, 0, NULL, 1, ?)
        """, (
            "NEW25",
            2,
            None,
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        ))
        
        con.executescript("""
        CREATE TABLE IF NOT EXISTS redeem_codes (
            id INTEGER PRIMARY KEY,
            code TEXT NOT NULL UNIQUE COLLATE NOCASE,
            credits INTEGER NOT NULL CHECK (credits > 0),
            max_uses INTEGER CHECK (
                max_uses IS NULL OR max_uses > 0
            ),
            uses INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS code_redemptions (
            id INTEGER PRIMARY KEY,
            code_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            redeemed_at TEXT NOT NULL,
            UNIQUE (code_id, user_id),
            FOREIGN KEY (code_id) REFERENCES redeem_codes(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
        """)

        # Add NEW25 on first startup; never overwrite an existing code.
        con.execute(
            """
            INSERT OR IGNORE INTO redeem_codes
                (code, credits, max_uses, uses, enabled, created_at)
            VALUES (?, ?, NULL, 0, 1, ?)
            """,
            ("NEW25", 2, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
init_db()

def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')

def current_user():
    uid = session.get('user_id')
    if not uid: return None
    with db() as con: return con.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user(): return jsonify(error='Please log in first.'), 401
        return fn(*a, **kw)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u = current_user()
        if not u: return jsonify(error='Please log in first.'), 401
        if u['username'].lower() not in ADMIN_USERNAMES: return jsonify(error='Admin access required.'), 403
        return fn(*a, **kw)
    return wrapper

def safe_package(p): return {k:p[k] for k in ('id','name','credits','price_display','featured')}

def apply_paid_checkout(session_id):
    """Apply credits once, after Stripe has verified that a checkout is paid."""
    with db() as con:
        row = con.execute('SELECT * FROM payments WHERE session_id=?', (session_id,)).fetchone()
        if not row: return False, "This checkout isn't linked to a Bolt account."
        if row['credited']: return True, 'Credits already added.'
        package = next((p for p in PACKAGES if p['id'] == row['package_id']), None)
        if not package: return False, 'The credit package no longer exists. Contact support.'
        changed = con.execute('UPDATE payments SET credited=1 WHERE session_id=? AND credited=0', (session_id,)).rowcount
        if changed: con.execute('UPDATE users SET credits=credits+? WHERE id=?', (package['credits'], row['user_id']))
        return True, 'Credits added!'

@app.post("/api/admin/gift-credits")
@admin_required
def gift_credits():
    data = request.get_json(silent=True) or {}

    username = data.get("username")
    credits = data.get("credits")

    if not isinstance(username, str) or not username.strip():
        return jsonify({"error": "Enter a username."}), 400

    if (
        not isinstance(credits, int)
        or isinstance(credits, bool)
        or not 1 <= credits <= 1_000_000
    ):
        return jsonify({"error": "Enter a valid credit amount."}), 400

    admin = current_user()

    with db() as con:
        con.execute("BEGIN IMMEDIATE")

        recipient = con.execute(
            "SELECT id, username FROM users WHERE username = ? COLLATE NOCASE",
            (username.strip(),)
        ).fetchone()

        if recipient is None:
            return jsonify({"error": "That user does not exist."}), 404

        con.execute(
            "UPDATE users SET credits = credits + ? WHERE id = ?",
            (credits, recipient["id"])
        )

        con.execute("""
            INSERT INTO credit_gifts
                (admin_user_id, recipient_user_id, credits, created_at)
            VALUES (?, ?, ?, ?)
        """, (admin["id"], recipient["id"], credits, now()))

    return jsonify({
        "success": True,
        "username": recipient["username"],
        "credits_gifted": credits
    })


@app.get("/api/admin/gift-history")
@admin_required
def gift_history():
    with db() as con:
        rows = con.execute("""
            SELECT
                g.credits,
                g.created_at,
                a.username AS admin_username,
                r.username AS recipient_username
            FROM credit_gifts g
            JOIN users a ON a.id = g.admin_user_id
            JOIN users r ON r.id = g.recipient_user_id
            ORDER BY g.id DESC
            LIMIT 100
        """).fetchall()

    return jsonify({
        "gifts": [dict(row) for row in rows]
    })




@app.post("/api/admin/reset-all-data")
@admin_required
def reset_all_data():
    try:
        with db() as con:
            con.execute("BEGIN IMMEDIATE")

            # Delete dependent records before their parent records.
            con.execute("DELETE FROM code_redemptions")
            con.execute("DELETE FROM credit_gifts")
            con.execute("DELETE FROM payments")
            con.execute("DELETE FROM jobs")
            con.execute("DELETE FROM redeem_codes")
            con.execute("DELETE FROM users")
            con.execute("""
                INSERT INTO redeem_codes
                    (code, credits, max_uses, uses, expires_at, enabled, created_at)
                VALUES (?, ?, ?, 0, NULL, 1, ?)
            """, (
                "NEW25",
                2,
                None,
                datetime.now(timezone.utc).isoformat(timespec="seconds")
            ))

            # Reset auto-increment counters for these tables.
            con.execute("""
                DELETE FROM sqlite_sequence
                WHERE name IN (
                    'users',
                    'jobs',
                    'payments',
                    'redeem_codes',
                    'code_redemptions',
                    'credit_gifts'
                )
            """)

        return jsonify({
            "success": True,
            "message": "All application data has been reset."
        })

    except Exception:
        app.logger.exception("Database reset failed")
        return jsonify({
            "error": "Database reset failed. Check the server log."
        }), 500




@app.get('/')
def index(): return send_from_directory(APP_DIR, 'index.html')
@app.get('/usage')
def usage(): return send_from_directory(APP_DIR, 'usage.html')
@app.get('/buy')
def buy(): return send_from_directory(APP_DIR, 'buy.html')
@app.get('/payment/success')
def payment_success(): return send_from_directory(APP_DIR, 'payment_success.html')
@app.get('/admin')
def admin_page():
    u=current_user()
    if not u: return redirect('/login?next=/admin')
    if u['username'].lower() not in ADMIN_USERNAMES: return 'Forbidden', 403
    return send_from_directory(APP_DIR, 'admin.html')
@app.get('/login')
def login_page(): return send_from_directory(APP_DIR, 'login.html')
@app.get('/signup')
def signup_page(): return send_from_directory(APP_DIR, 'signup.html')
@app.get('/logout')
def logout(): session.clear(); return redirect('/?logged_out=1')

@app.post('/api/signup')
def signup():
    data=request.get_json(silent=True) or {}; username=str(data.get('username','')).strip(); password=str(data.get('password',''))
    if not 2 <= len(username) <= 30 or not username.replace('_','').isalnum(): return jsonify(error='Use a 2–30 character username with letters, numbers, or underscores.'),400
    if len(password)<10: return jsonify(error='Password must be at least 10 characters.'),400
    try:
        with db() as con:
            con.execute('INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)',(username,generate_password_hash(password),now()))
            uid=con.execute('SELECT id FROM users WHERE username=?',(username,)).fetchone()['id']
    except sqlite3.IntegrityError: return jsonify(error='That username is already registered.'),409
    session.clear(); session['user_id']=uid; session.permanent=True
    return jsonify(ok=True)

@app.post('/api/login')
def login():
    data=request.get_json(silent=True) or {}; username=str(data.get('username','')).strip()
    with db() as con: u=con.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE',(username,)).fetchone()
    if not u or not check_password_hash(u['password_hash'],str(data.get('password',''))): return jsonify(error='Incorrect username or password.'),401
    session.clear(); session['user_id']=u['id']; session.permanent=True
    return jsonify(ok=True)

@app.get('/api/me')
def me():
    u=current_user()
    if not u: return jsonify(error='Not logged in.'),401
    with db() as con: jobs=con.execute('SELECT target,bots,status,created_at FROM jobs WHERE user_id=? ORDER BY id DESC LIMIT 20',(u['id'],)).fetchall()
    return jsonify(username=u['username'],credits=u['credits'],is_admin=u['username'].lower() in ADMIN_USERNAMES,jobs=[dict(j) for j in jobs])

@app.get('/api/packages')
def packages(): return jsonify([safe_package(p) for p in PACKAGES])

@app.post('/api/jobs')
@login_required
def create_job():
    data=request.get_json(silent=True) or {}; target=str(data.get('target','')).strip()
    try: bots=int(data.get('bots',1))
    except (ValueError,TypeError): return jsonify(error='Bot count must be a number.'),400
    if not target or len(target)>200: return jsonify(error='Enter a task input under 200 characters.'),400
    if not 1 <= bots <= 25: return jsonify(error='Choose between 1 and 25 bots.'),400
    cost=bots*COST_PER_BOT; u=current_user()
    with db() as con:
        cur=con.execute('UPDATE users SET credits=credits-? WHERE id=? AND credits>=?',(cost,u['id'],cost))
        if cur.rowcount!=1: return jsonify(error=f'Not enough credits. This task costs {cost} credits.'),402
        cur = con.execute(
            "INSERT INTO jobs(user_id,target,bots,status,created_at) "
            "VALUES(?,?,?,?,?)",
            (u["id"], target, bots, "queued", now()),
        )
        job_id = cur.lastrowid
    try:
        task_executor.submit(run_job, job_id)
    except Exception:
        app.logger.exception("Could not queue job %s", job_id)

        with db() as con:
            con.execute(
                "UPDATE jobs SET status = 'failed' "
                "WHERE id = ? AND status = 'queued'",
                (job_id,),
            )
            con.execute(
                "UPDATE users SET credits = credits + ? "
                "WHERE id = ?",
                (cost, u["id"]),
            )

        return jsonify(
            error="The task could not be queued. Your credits have been refunded."
        ), 503

    return jsonify(ok=True, cost=cost, job_id=job_id)

@app.post('/api/checkout')
@login_required
def checkout():
    if not stripe.api_key: return jsonify(error="Stripe isn't configured. Set STRIPE_SECRET_KEY to your sk_test_ key."),503
    data=request.get_json(silent=True) or {}; package=next((p for p in PACKAGES if p['id']==data.get('package_id')),None)
    if not package: return jsonify(error='Unknown package.'),400
    u=current_user()
    try:
        cs=stripe.checkout.Session.create(mode='payment',line_items=[{'price_data':{'currency':'aud','product_data':{'name':f"Bolt {package['name']} Credit Pack"},'unit_amount':package['amount']},'quantity':1}],success_url=PUBLIC_BASE_URL+'/payment/success?session_id={CHECKOUT_SESSION_ID}',cancel_url=PUBLIC_BASE_URL+'/buy?payment=cancelled',client_reference_id=str(u['id']),metadata={'user_id':str(u['id']),'package_id':package['id']})
        with db() as con: con.execute('INSERT OR IGNORE INTO payments(session_id,user_id,package_id,credited,created_at) VALUES(?,?,?,0,?)',(cs.id,u['id'],package['id'],now()))
        return jsonify(url=cs.url)
    except Exception:
        app.logger.exception('Stripe checkout failed')
        return jsonify(error='Could not create Stripe checkout. Check the test key and server log.'),502

@app.get('/api/checkout/confirm')
def confirm_checkout():
    sid=str(request.args.get('session_id','')).strip()
    if not sid or not stripe.api_key: return jsonify(ok=False,error='Missing checkout session or Stripe configuration.'),400
    try:
        cs=stripe.checkout.Session.retrieve(sid)
        if cs.mode != 'payment' or cs.payment_status != 'paid': return jsonify(ok=False,pending=True,message="Stripe hasn't confirmed payment yet."),202
        success,message=apply_paid_checkout(sid)
        if not success: return jsonify(ok=False,error=message),400
        return jsonify(ok=True,message=message)
    except Exception:
        app.logger.exception('Could not verify Stripe checkout')
        return jsonify(ok=False,error='Could not verify payment with Stripe yet. The webhook can still confirm it.'),502

@app.post('/stripe/webhook')
def stripe_webhook():
    if not STRIPE_WEBHOOK_SECRET: return 'Webhook not configured',503
    try: event=stripe.Webhook.construct_event(request.get_data(),request.headers.get('Stripe-Signature',''),STRIPE_WEBHOOK_SECRET)
    except Exception: return 'Invalid webhook signature',400
    if event['type'] in ('checkout.session.completed','checkout.session.async_payment_succeeded'):
        obj=event['data']['object']
        if obj.get('payment_status')=='paid': apply_paid_checkout(obj['id'])
    return jsonify(received=True)

@app.get('/api/admin')
@admin_required
def admin_api():
    with db() as con: jobs=con.execute('SELECT jobs.target,jobs.bots,jobs.status,jobs.created_at,users.username FROM jobs JOIN users ON users.id=jobs.user_id ORDER BY jobs.id DESC LIMIT 100').fetchall()
    return jsonify(packages=[safe_package(p) for p in PACKAGES],jobs=[dict(j) for j in jobs])


@app.delete('/api/account')
@login_required
def delete_account():
    user_id = session.get('user_id')

    if not user_id:
        return jsonify({
            'error': 'You must be logged in.'
        }), 401

    try:
        with db() as con:
            # Remove the user's related records first.
            con.execute(
                'DELETE FROM jobs WHERE user_id = ?',
                (user_id,)
            )
            con.execute(
                'DELETE FROM payments WHERE user_id = ?',
                (user_id,)
            )
            con.execute(
                'DELETE FROM users WHERE id = ?',
                (user_id,)
            )

        # End the user's session.
        session.clear()

        return jsonify({
            'success': True,
            'message': 'Your account has been deleted.'
        }), 200

    except Exception:
        app.logger.exception(
            'Failed to delete account for user %s',
            user_id
        )
        return jsonify({
            'error': 'Could not delete your account. Please try again.'
        }), 500

@app.post("/api/redeem")
@login_required
def redeem_code():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip().upper()
    user = current_user()

    if not re.fullmatch(r"[A-Z0-9_-]{3,40}", code):
        return jsonify(error="Enter a valid redeem code."), 400

    try:
        with db() as con:
            # Serialize claims so the last global use cannot be
            # claimed by two accounts at the same time.
            con.execute("BEGIN IMMEDIATE")

            row = con.execute(
                "SELECT * FROM redeem_codes WHERE code = ?",
                (code,),
            ).fetchone()

            if row is None or not row["enabled"]:
                return jsonify(error="That code is invalid or disabled."), 400

            if (
                row["max_uses"] is not None
                and row["uses"] >= row["max_uses"]
            ):
                return jsonify(
                    error="This code has reached its global redemption limit."
                ), 409

            if row["expires_at"]:
                expiry = datetime.fromisoformat(row["expires_at"])

                if expiry.tzinfo is None:
                    expiry = expiry.replace(tzinfo=timezone.utc)

                if expiry <= datetime.now(timezone.utc):
                    return jsonify(error="This code has expired."), 400

            already_used = con.execute(
                """
                SELECT 1 FROM code_redemptions
                WHERE code_id = ? AND user_id = ?
                """,
                (row["id"], user["id"]),
            ).fetchone()

            if already_used:
                return jsonify(
                    error="You've already redeemed this code."
                ), 409

            # Recheck the limit atomically before awarding credits.
            changed = con.execute(
                """
                UPDATE redeem_codes
                SET uses = uses + 1
                WHERE id = ?
                  AND enabled = 1
                  AND (max_uses IS NULL OR uses < max_uses)
                """,
                (row["id"],),
            ).rowcount

            if changed != 1:
                return jsonify(
                    error="This code has reached its redemption limit."
                ), 409

            con.execute(
                """
                INSERT INTO code_redemptions
                    (code_id, user_id, redeemed_at)
                VALUES (?, ?, ?)
                """,
                (row["id"], user["id"], now()),
            )

            con.execute(
                "UPDATE users SET credits = credits + ? WHERE id = ?",
                (row["credits"], user["id"]),
            )

        return jsonify(
            ok=True,
            credits_awarded=row["credits"],
            message=f"Success! You received {row['credits']} credits.",
        )

    except sqlite3.IntegrityError:
        return jsonify(
            error="This code has already been redeemed by your account."
        ), 409
    except Exception:
        app.logger.exception("Redeem-code processing failed")
        return jsonify(error="Could not redeem the code. Try again."), 500


@app.get("/api/admin/redeem-codes")
@admin_required
def list_redeem_codes():
    with db() as con:
        rows = con.execute(
            """
            SELECT
                c.id, c.code, c.credits, c.max_uses, c.uses,
                c.expires_at, c.enabled, c.created_at,
                (
                    SELECT COUNT(*)
                    FROM code_redemptions r
                    WHERE r.code_id = c.id
                ) AS successful_redemptions
            FROM redeem_codes c
            ORDER BY c.id DESC
            """
        ).fetchall()

    return jsonify(codes=[dict(row) for row in rows])


@app.post("/api/admin/redeem-codes")
@admin_required
def create_redeem_code():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip().upper()

    if not re.fullmatch(r"[A-Z0-9_-]{3,40}", code):
        return jsonify(
            error="Codes must be 3–40 characters: letters, numbers, _ or -."
        ), 400

    try:
        credits = int(data.get("credits", 0))
    except (TypeError, ValueError):
        return jsonify(error="Credits must be a whole number."), 400

    if not 1 <= credits <= 1_000_000:
        return jsonify(error="Credits must be between 1 and 1,000,000."), 400

    raw_limit = data.get("max_uses")

    if raw_limit in (None, "", "unlimited"):
        max_uses = None
    else:
        try:
            max_uses = int(raw_limit)
        except (TypeError, ValueError):
            return jsonify(error="The redemption limit must be a whole number."), 400

        if not 1 <= max_uses <= 1_000_000:
            return jsonify(error="Use a limit between 1 and 1,000,000."), 400

    raw_expiry = str(data.get("expires_at", "")).strip()
    expires_at = None

    if raw_expiry:
        try:
            expiry = datetime.fromisoformat(raw_expiry)

            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)

            expires_at = expiry.astimezone(timezone.utc).isoformat(
                timespec="seconds"
            )

            if expiry <= datetime.now(timezone.utc):
                return jsonify(error="Expiry must be in the future."), 400
        except ValueError:
            return jsonify(error="Invalid expiry date."), 400

    try:
        with db() as con:
            cur = con.execute(
                """
                INSERT INTO redeem_codes
                    (code, credits, max_uses, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (code, credits, max_uses, expires_at, now()),
            )

            code_id = cur.lastrowid

        return jsonify(ok=True, id=code_id), 201

    except sqlite3.IntegrityError:
        return jsonify(error="A code with that name already exists."), 409

@app.get("/redeem")
def redeem_page():
    if not current_user():
        return redirect("/login?next=/redeem")

    return send_from_directory(APP_DIR, "redeem.html")

@app.get("/api/redeem/status")
@login_required
def redeem_status():
    user = current_user()

    with db() as con:
        row = con.execute("""
            SELECT 1
            FROM code_redemptions cr
            JOIN redeem_codes rc ON rc.id = cr.code_id
            WHERE cr.user_id = ? AND rc.code = ?
        """, (user["id"], "NEW25")).fetchone()

    return jsonify({
        "new25_redeemed": row is not None
    })

@app.patch("/api/admin/redeem-codes/<int:code_id>")
@admin_required
def update_redeem_code(code_id):
    data = request.get_json(silent=True) or {}

    if not isinstance(data.get("enabled"), bool):
        return jsonify(error="Provide enabled as true or false."), 400

    with db() as con:
        changed = con.execute(
            "UPDATE redeem_codes SET enabled = ? WHERE id = ?",
            (int(data["enabled"]), code_id),
        ).rowcount

    if not changed:
        return jsonify(error="Redeem code not found."), 404

    return jsonify(ok=True)

if __name__=='__main__': app.run(host='127.0.0.1',port=int(os.environ.get('PORT','5000')),debug=False)