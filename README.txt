BOLT STARTER — UPDATED LOCAL VERSION

1. Install Python 3.13 and open PowerShell in this folder.
2. Run: py -3.13 -m pip install -r requirements.txt
3. Set your Stripe TEST secret key (never a live key):
   $env:STRIPE_SECRET_KEY = "sk_test_..."
4. Set the username that should be admin:
   $env:ADMIN_USERNAMES = "your_username"
5. For local Stripe redirect URLs:
   $env:PUBLIC_BASE_URL = "http://127.0.0.1:5000"
6. Run: py -3.13 backend.py
7. Open http://127.0.0.1:5000 and create a Bolt account.

PAYMENTS:
- The success page verifies the Checkout Session with Stripe server-side and credits the account once.
- A signed webhook can also confirm credits. For local testing, install/login to Stripe CLI, then run:
  stripe listen --forward-to localhost:5000/stripe/webhook
  Copy the whsec_... value to STRIPE_WEBHOOK_SECRET before starting the server.
- Use Stripe test mode and test cards only. Never paste secret keys into chat or frontend code.

DATA AND LOGIN:
- Accounts, password hashes, credits, tasks and checkout records are stored in bolt.sqlite3 in this folder.
- Passwords are hashed. Local sessions use .bolt_session_secret so they survive restarts if FLASK_SECRET_KEY is not explicitly set.
- Keep bolt.sqlite3 and .bolt_session_secret private. Back them up before deleting/resetting the project.

LIMITS:
- The task runner is a demo queue; it does not run real bots.
- Package prices are configured in backend.py.
- This is a local development starter, not production-ready. Before public launch add HTTPS, CSRF protection, rate limiting, backups, a production server, and review payment/tax/business requirements.
