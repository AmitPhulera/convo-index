"""acme-shop: a fictional Django storefront (work). Tickets SHOP-*, PRs on acme-labs/acme-shop."""
from demo.base import A, S, T, U, conv

R = "acme-shop"
PR = "https://github.com/acme-labs/acme-shop/pull/"

CONVERSATIONS = [
    conv("acme-01", "claude_code", "claude-sonnet-4-6", "2026-09-02T09:14:00", R,
         "Flaky checkout test on CI", branch="fix/SHOP-142-flaky-checkout", turns=[
        U("SHOP-142: test_checkout_applies_coupon fails maybe 1 in 10 runs on CI but never locally. Can you find out why?"),
        A("Let me run it in a loop with random ordering first.",
          thinking="Intermittent + CI-only usually means test ordering, time, or shared state. pytest-randomly is installed, so order is the first suspect.",
          tools=[T("Bash", "pytest shop/tests/test_checkout.py -p randomly -x --count 30 -q",
                   "....F\nFAILED shop/tests/test_checkout.py::test_checkout_applies_coupon\n"
                   "AssertionError: assert Decimal('90.00') == Decimal('81.00')\n1 failed, 4 passed")]),
        A("Reproduced. The coupon is applied once instead of twice when the test runs after "
          "`test_coupon_expiry`. Checking how coupons are cached.",
          tools=[T("Grep", {"pattern": "lru_cache|cache.get", "path": "shop/pricing"},
                   "shop/pricing/coupons.py:18:@lru_cache(maxsize=128)\nshop/pricing/coupons.py:19:def active_coupon(code):"),
                 T("Read", "shop/pricing/coupons.py",
                   "@lru_cache(maxsize=128)\ndef active_coupon(code):\n    return Coupon.objects.filter(code=code, expires_at__gt=now()).first()")]),
        A("Found it: `active_coupon` is wrapped in `functools.lru_cache`, so a Coupon row cached by an earlier "
          "test survives into later tests, after the database was rolled back. On CI the order is "
          "randomised, so it only fails when `test_coupon_expiry` runs first.\n\n"
          "Two options:\n1. Clear the cache in a pytest fixture (hides it in tests only).\n"
          "2. Drop `lru_cache` and cache per request instead. A process-wide cache of DB rows is also wrong "
          "in production: an expired coupon stays usable until the worker restarts.\n\nI'd do 2."),
        U("Agreed, do 2."),
        A("Replaced the `lru_cache` with a per-request memo on `request.pricing_cache`, and added a regression test "
          "that expires a coupon mid-process. 30 random-order runs pass.",
          tools=[T("Edit", {"file_path": "shop/pricing/coupons.py", "old_string": "@lru_cache(maxsize=128)\n", "new_string": ""}),
                 T("Bash", "pytest shop/tests -p randomly --count 30 -q", "1230 passed in 48.12s")]),
    ], summary=S("test_checkout_applies_coupon failed intermittently on CI (SHOP-142).",
                 "A process-wide lru_cache on active_coupon leaked rows between tests; replaced with a per-request cache.",
                 "lru_cache on ORM lookups outlives the transaction: stale data in tests and in production.")),

    conv("acme-02", "codex", "gpt-5.6-sol", "2026-09-04T14:02:00", R,
         "Add Stripe webhook idempotency", branch="feat/SHOP-151-webhook-idempotency", turns=[
        U("We sometimes ship an order twice when Stripe retries `checkout.session.completed`. SHOP-151. "
          "Make the webhook handler idempotent."),
        A("Looking at the handler and the Order model.",
          tools=[T("shell", "rg -n 'checkout.session.completed' shop/",
                   "shop/payments/webhooks.py:41:    if event.type == \"checkout.session.completed\":")]),
        A("The handler creates a Shipment every time the event arrives. I'll record processed event ids in a "
          "`ProcessedWebhook` table with a unique constraint and wrap the handler in a transaction, so a retry "
          "hits the constraint and returns 200 without side effects.",
          tools=[T("apply_patch", {"patch": "*** Update File: shop/payments/webhooks.py\n+    with transaction.atomic():\n+        ProcessedWebhook.objects.create(event_id=event.id)"}),
                 T("shell", "python manage.py makemigrations payments && pytest shop/payments -q",
                   "Migrations for 'payments':\n  0014_processedwebhook.py\n42 passed in 3.90s")]),
        U("What happens if two retries arrive at the same moment?"),
        A("Both open a transaction; the second INSERT blocks on the unique index until the first commits, then "
          "raises IntegrityError, which the handler catches and answers 200. Only one Shipment is created. "
          "I added a test that fires the same event from two threads to pin that down."),
    ], summary=S("Stripe retries of checkout.session.completed shipped orders twice (SHOP-151).",
                 "Processed event ids go into a uniquely-constrained table inside the handler's transaction.",
                 "Idempotency keys belong in the same transaction as the side effect.")),

    conv("acme-03", "claude_code", "claude-opus-4-8", "2026-09-09T10:30:00", R,
         "Slow product listing page", branch="perf/SHOP-160-listing-queries", turns=[
        U("The /products page takes 2.5s with 48 items. SHOP-160. Profile it?"),
        A("Counting queries with the Django test client first.",
          tools=[T("Bash", "python manage.py shell -c \"from django.test.utils import CaptureQueriesContext; ...\"",
                   "queries: 147")]),
        A("147 queries for 48 products: the template touches `product.brand`, `product.images.first` and "
          "`product.stock_level` per item. Classic N+1.\n\nAdding `select_related('brand')`, "
          "`prefetch_related(Prefetch('images', queryset=Image.objects.order_by('position')))` and annotating "
          "stock instead of a property that queries.",
          tools=[T("Edit", {"file_path": "shop/catalog/views.py", "old_string": "Product.objects.filter(active=True)",
                            "new_string": "Product.objects.filter(active=True).select_related('brand').prefetch_related(...)"}),
                 T("Bash", "pytest shop/catalog -q && python scripts/count_queries.py /products",
                   "88 passed in 6.02s\nqueries: 4  time: 180ms")]),
        A("Down to 4 queries and ~180ms. I also added `assertNumQueries(4)` to the view test so it can't regress. "
          f"Opened {PR}212."),
    ], summary=S("Product listing took 2.5s due to 147 queries (SHOP-160).",
                 "select_related/prefetch_related and an annotated stock level; 4 queries, 180ms; assertNumQueries guard.",
                 "Pin query counts in view tests.")),

    conv("acme-04", "opencode", "claude-sonnet-4-5", "2026-09-15T16:45:00", R,
         "Explain the order state machine", turns=[
        U("I'm new on acme-shop. How does an order move from cart to delivered? Point me at the code."),
        A("", tools=[T("read", "shop/orders/states.py",
                       "TRANSITIONS = {\n  'cart': ['pending_payment'],\n  'pending_payment': ['paid', 'cancelled'],\n"
                       "  'paid': ['packed', 'refunded'],\n  'packed': ['shipped'],\n  'shipped': ['delivered', 'returned'],\n}")]),
        A("Orders follow the table in `shop/orders/states.py`:\n\n"
          "`cart → pending_payment → paid → packed → shipped → delivered`, with `cancelled`, `refunded` and "
          "`returned` as side exits.\n\n"
          "- `Order.transition(to)` checks the table and writes an `OrderEvent` row, so the history is auditable.\n"
          "- `paid` is only set by the Stripe webhook (`shop/payments/webhooks.py`), never by the client.\n"
          "- `packed` and `shipped` come from the warehouse API (`shop/fulfilment/api.py`).\n\n"
          "Start with `tests/orders/test_transitions.py`; it's the most readable description of the rules."),
        U("Why can't a shipped order be refunded directly?"),
        A("By design: money goes back only after the parcel is `returned`, so the warehouse confirms receipt "
          "first. Support uses `returned → refunded` (see the `RETURN_TRANSITIONS` block further down)."),
    ], summary=S("Onboarding question: how orders move through states.",
                 "Walked through the TRANSITIONS table, who triggers each state, and why refunds need a return first.")),

    conv("acme-05", "claude_code", "claude-sonnet-4-6", "2026-09-23T11:05:00", R,
         "Upgrade to Django 5.2", branch="chore/django-5.2", turns=[
        U("Upgrade acme-shop from Django 4.2 to 5.2 and fix whatever breaks."),
        A("Bumping the pin and running the suite with deprecation warnings as errors.",
          tools=[T("Bash", "uv add 'django~=5.2' && pytest -W error::DeprecationWarning -q",
                   "FAILED shop/accounts/tests/test_login.py::test_logout_get - 405 != 302\n"
                   "FAILED shop/core/tests/test_forms.py::test_url_field - AssumeSchemeWarning\n2 failed, 1228 passed")]),
        A("Two breaks:\n1. Logout via GET was removed in 5.0; our header link does `<a href=/logout>`. "
          "Switched it to a small POST form.\n2. `forms.URLField` warns about the default scheme; set "
          "`assume_scheme='https'` on the two fields that use it.",
          tools=[T("Edit", {"file_path": "templates/partials/header.html", "old_string": "<a href=\"{% url 'logout' %}\">",
                            "new_string": "<form method=\"post\" action=\"{% url 'logout' %}\">{% csrf_token %}"}),
                 T("Bash", "pytest -W error::DeprecationWarning -q", "1230 passed in 51.30s")]),
        A(f"All green. PR: {PR}219. The changelog note mentions the logout change for the mobile app, which also "
          "calls GET /logout."),
    ], summary=S("Upgrade Django 4.2 → 5.2.",
                 "Logout moved to POST; URLField assume_scheme set; suite passes with deprecations as errors.",
                 "Django 5 removed GET logout; check API clients too.")),

    conv("acme-06", "pi", "claude-sonnet-4-6", "2026-10-01T09:40:00", R,
         "Search returns out-of-stock items first", branch="fix/SHOP-171-search-ranking", turns=[
        U("SHOP-171: searching 'linen shirt' shows sold-out products at the top. Fix the ranking."),
        A("", tools=[T("read", "shop/search/ranking.py",
                       "def score(hit):\n    return hit.text_score * 0.8 + hit.popularity * 0.2")]),
        A("Ranking ignores stock. I'll demote unavailable items instead of hiding them (people still want to see "
          "them, and hiding changes result counts): multiply by 0.3 when `stock == 0`, and sort ties by "
          "`restock_eta`.",
          tools=[T("edit", {"path": "shop/search/ranking.py"}),
                 T("bash", "pytest shop/search -q", "37 passed in 1.12s")]),
    ], summary=S("Sold-out products ranked first in search (SHOP-171).",
                 "Demote out-of-stock hits by 0.3x instead of hiding them; ties broken by restock ETA.")),

    conv("acme-07", "antigravity", "", "2026-10-07T13:20:00", R,
         "Write a data migration for split addresses", branch="feat/SHOP-175-address-fields", turns=[
        U("SHOP-175: split Address.line into line1/line2 with a data migration. Production has ~2M rows."),
        A("I'll add the columns nullable, backfill in batches of 10k with `iterator()`, then make line1 required "
          "in a separate migration so the deploy is reversible.",
          tools=[T("run_command", "python manage.py makemigrations accounts --empty -n split_address_lines",
                   "Migrations for 'accounts':\n  0031_split_address_lines.py")]),
        A("The backfill splits on the first newline or comma, keeps the original in `line_legacy` for one release, "
          "and logs rows it couldn't split. On a 2M-row copy it ran in 4m10s without locking the table."),
    ], summary=S("Split a single address line into two fields on a 2M-row table (SHOP-175).",
                 "Three-step migration: nullable columns, batched backfill keeping the legacy value, then NOT NULL.")),

    conv("acme-08", "codex", "gpt-5.5", "2026-10-20T15:10:00", R,
         "Review PR 231: gift cards", turns=[
        U(f"Review {PR}231 (gift cards). Focus on money handling."),
        A("", tools=[T("shell", "gh pr diff 231 | head -200", "+    balance = models.FloatField(default=0)\n+    def redeem(self, amount):\n+        self.balance -= amount\n+        self.save()")]),
        A("Three blocking issues:\n\n1. `balance` is a FloatField. Use `DecimalField(max_digits=10, decimal_places=2)`: "
          "0.1 + 0.2 already drifts.\n2. `redeem` reads, subtracts and saves without a lock: two concurrent checkouts "
          "can both spend the same balance. Use `select_for_update()` or an `F('balance') - amount` update with a "
          "`balance__gte=amount` filter.\n3. No ledger: once the balance changes there's no record of why. Add a "
          "`GiftCardTransaction` row per redeem/refund.\n\nNon-blocking: the code generator uses `random`; use "
          "`secrets.token_urlsafe`."),
    ], summary=S("Review of the gift-card PR.",
                 "Flagged float money, a race in redeem, missing ledger, and non-cryptographic codes.")),

    conv("acme-09", "claude_code", "claude-haiku-4-5", "2026-11-03T08:55:00", R,
         "Translate the order confirmation email", turns=[
        U("Add a German version of templates/emails/order_confirmed.html."),
        A("Created `order_confirmed.de.html` using the existing `{% blocktrans %}` blocks and added the strings to "
          "`locale/de/LC_MESSAGES/django.po`. `compilemessages` is clean.",
          tools=[T("Bash", "python manage.py compilemessages -l de", "processing file django.po in locale/de/LC_MESSAGES")]),
    ], summary=S("German translation of the order confirmation email.", "New template plus .po entries; compiles cleanly.")),
]
