"""ledgerly: a fictional Go billing API (work). Tickets LED-*, PRs on acme-labs/ledgerly."""
from demo.base import A, S, T, U, conv

R = "ledgerly"
PR = "https://github.com/acme-labs/ledgerly/pull/"

CONVERSATIONS = [
    conv("led-01", "codex", "gpt-5.6-sol", "2026-09-03T10:20:00", R,
         "Goroutine leak in invoice exporter", branch="fix/LED-88-exporter-leak", turns=[
        U("LED-88: the exporter's memory grows ~40MB/hour until it's OOM-killed. pprof attached in the ticket shows "
          "thousands of goroutines in `exportBatch`."),
        A("", tools=[T("shell", "rg -n 'go func' internal/export/",
                       "internal/export/batch.go:57:\tgo func() { results <- render(inv) }()")]),
        A("Each invoice starts a goroutine that sends on an unbuffered `results` channel. When the context is "
          "cancelled (timeout on a slow batch), the reader returns early and those senders block forever.\n\n"
          "Fix: make the send select on `ctx.Done()`, and use an `errgroup` with `SetLimit(8)` instead of "
          "one goroutine per invoice.",
          tools=[T("apply_patch", {"patch": "*** Update File: internal/export/batch.go\n-\tgo func() { results <- render(inv) }()\n+\tg.Go(func() error { ... })"}),
                 T("shell", "go test ./internal/export/ -run TestExportCancel -count=50 -race",
                   "ok  \tledgerly/internal/export\t3.412s")]),
        A("Added `TestExportCancel`, which cancels mid-batch and checks `runtime.NumGoroutine()` returns to the "
          f"baseline. 50 runs with -race pass. {PR}74"),
    ], summary=S("Invoice exporter leaked goroutines until OOM (LED-88).",
                 "Senders blocked on an unbuffered channel after cancellation; errgroup with a limit and ctx-aware sends.",
                 "Every goroutine that sends needs a way out when the reader leaves.")),

    conv("led-02", "claude_code", "claude-opus-4-8", "2026-09-17T13:00:00", R,
         "Design proration for mid-cycle plan changes", turns=[
        U("Product wants customers to switch plans mid-cycle with proration (LED-95). Before coding: what are the "
          "options and edge cases?"),
        A("Three common models:\n\n"
          "1. **Immediate proration**: credit unused time on the old plan, charge the new plan for the rest of the "
          "cycle now. Simple to explain; creates many small invoices.\n"
          "2. **Next-invoice proration**: compute the same amounts but add them as line items to the next invoice. "
          "Fewer charges; customers see a surprising next bill.\n"
          "3. **Change at renewal**: no proration; downgrades usually work this way.\n\n"
          "Edge cases to decide:\n- Upgrade then downgrade in the same cycle (net it out or two credits?)\n"
          "- Trials: proration against a $0 period\n- Annual plans: 11 months of credit is a lot of money; refund or credit?\n"
          "- Currency rounding: allocate the remainder cent to the last line\n- Tax: credits must reverse tax at the "
          "original rate, not today's.\n\nRecommendation: upgrades immediate (1), downgrades at renewal (3).",
          thinking="They asked for options before code. Keep it to a decision memo, flag tax and rounding since billing bugs there are costly."),
        U("Go with that. Write the calculation as a pure function first."),
        A("Added `proration.Calculate(oldPlan, newPlan, cycleStart, cycleEnd, at)` returning line items in "
          "minor units (int64 cents), with table tests for mid-cycle upgrade, same-day change, leap-year "
          "February and a 3-way currency rounding case.",
          tools=[T("Bash", "go test ./internal/proration/ -v | tail -5",
                   "--- PASS: TestCalculate/leap_february (0.00s)\n--- PASS: TestCalculate/rounding_remainder (0.00s)\nPASS\nok  \tledgerly/internal/proration\t0.011s")]),
    ], summary=S("How to prorate plan changes mid-cycle (LED-95).",
                 "Upgrades prorate immediately, downgrades apply at renewal; pure Calculate function in cents with table tests.",
                 "Decide tax reversal and rounding before writing the code.")),

    conv("led-03", "pi", "gpt-5.4", "2026-10-02T17:30:00", R,
         "Postgres deadlock in payment allocation", branch="fix/LED-102-deadlock", turns=[
        U("We get `deadlock detected` a few times a day in AllocatePayment. LED-102."),
        A("", tools=[T("bash", "grep -n 'FOR UPDATE' internal/payments/*.go",
                       "internal/payments/allocate.go:33:  SELECT ... FROM invoices WHERE id = ANY($1) FOR UPDATE")]),
        A("Two allocations lock the same invoices in different orders (the id list comes from the request as-is). "
          "Sorting the ids before `FOR UPDATE` gives every transaction the same lock order, which removes the cycle.",
          tools=[T("edit", {"path": "internal/payments/allocate.go"}),
                 T("bash", "go test ./internal/payments/ -run TestConcurrentAllocate -count=20",
                   "ok  \tledgerly/internal/payments\t8.901s")]),
    ], summary=S("Intermittent deadlocks when allocating payments (LED-102).",
                 "Lock invoices in a consistent (sorted) order before SELECT … FOR UPDATE.")),

    conv("led-04", "opencode", "deepseek-v4-pro", "2026-10-14T11:15:00", R,
         "Generate OpenAPI spec from handlers", turns=[
        U("Can we generate the OpenAPI spec from our chi handlers instead of hand-editing openapi.yaml?"),
        A("Options: annotate handlers for `swag` (comments, drifts easily), or define routes with `huma`, which "
          "derives the spec from typed request/response structs. Since you already use typed structs, huma is a "
          "smaller step: wrap the router once, then port handlers one at a time. Both specs can be served side "
          "by side during the migration."),
        U("Port the /invoices routes as a trial."),
        A("Ported the 4 invoice routes; `GET /openapi.json` now includes them with schemas generated from "
          "`InvoiceResponse`. The diff against the old yaml shows two fields that were documented but never "
          "returned (`memo`, `po_number`).",
          tools=[T("bash", "go run ./cmd/specdiff old/openapi.yaml http://localhost:8080/openapi.json",
                   "- invoices.InvoiceResponse.memo (documented, not returned)\n- invoices.InvoiceResponse.po_number (documented, not returned)")]),
    ], summary=S("Hand-edited OpenAPI spec drifted from the code.",
                 "Trial port of /invoices to huma; generated spec revealed two documented-but-missing fields.",
                 resolved="Partial")),

    conv("led-05", "claude_code", "claude-sonnet-4-6", "2026-11-05T09:00:00", R,
         "Flaky time-dependent test around midnight UTC", branch="fix/LED-110-midnight-test", turns=[
        U("TestDueDate fails only when CI runs between 00:00 and 01:00 UTC. LED-110."),
        A("The test computes \"tomorrow\" with `time.Now()` while the code uses the account's timezone. Between "
          "midnight UTC and midnight in Europe/Berlin they disagree on the date. I injected a `clock.Clock` into "
          "the due-date service and the test now pins `2026-03-29T00:30:00Z`, which also covers the DST switch.",
          tools=[T("Bash", "go test ./internal/dunning/ -run TestDueDate -v",
                   "=== RUN   TestDueDate/dst_spring_forward\n--- PASS: TestDueDate (0.00s)\nok")]),
    ], summary=S("A test failed only during one hour of the day (LED-110).",
                 "Injected a clock and pinned the time, including a DST boundary case.")),
]
