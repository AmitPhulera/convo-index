"""garden-planner: a fictional Next.js app for planning vegetable beds (personal)."""
from demo.base import A, S, T, U, conv

R = "garden-planner"
GH = "https://github.com/plotwise/garden-planner/"

CONVERSATIONS = [
    conv("gp-01", "claude_code", "claude-sonnet-4-6", "2026-09-13T15:00:00", R,
         "Hydration error on the bed planner page", branch="fix/hydration-dates", turns=[
        U("The planner page logs `Text content does not match server-rendered HTML` and the sowing dates flicker."),
        A("Dates are formatted with `toLocaleDateString()` on both server (UTC, en-US) and client (the user's "
          "locale and zone), so the HTML differs.",
          tools=[T("Grep", {"pattern": "toLocaleDateString", "path": "app"},
                   "app/beds/[id]/SowingList.tsx:31:  {d.toLocaleDateString()}")]),
        A("Moved the formatting into a small client component that renders an ISO date on the server and the "
          "localized one after mount, and gave it `suppressHydrationWarning`. The flicker is gone.",
          tools=[T("Bash", "pnpm test && pnpm build", "Tests: 41 passed\n✓ Compiled successfully")]),
    ], summary=S("Hydration mismatch on dates.", "Locale formatting moved client-side after mount.")),

    conv("gp-02", "antigravity", "", "2026-10-04T11:30:00", R,
         "Companion planting rules engine", turns=[
        U("I want warnings when incompatible plants share a bed (e.g. fennel next to tomatoes). How should I model it?"),
        A("Keep it data, not code: a `companions.json` with `good` and `bad` pairs plus a short reason, and a pure "
          "function `checkBed(plants) -> Warning[]` that checks each pair once. Store pairs sorted so "
          "`tomato/fennel` and `fennel/tomato` are the same key. The UI then shows a badge per conflicting pair.",
          tools=[T("run_command", "pnpm vitest run lib/companions", "✓ lib/companions.test.ts (9 tests)")]),
    ], summary=S("Warn about incompatible plants in a bed.", "Pair table in JSON and a pure check function with tests.")),

    conv("gp-03", "pi", "gpt-5.4", "2026-10-18T09:45:00", R,
         "Frost date lookup by postcode", turns=[
        U("Show the average last frost date for the user's postcode."),
        A("There's no free frost-date API for every country, so: ship a small table of climate-station "
          "averages (~2,000 stations, 60 KB gzipped), geocode the postcode once with the browser's coarse "
          "location or a postcode centroid file, and pick the nearest station. Everything works offline."),
        U("Fine, build it for the UK first."),
        A("Added `data/frost/uk.json` and `lib/frost.ts` (haversine nearest-station). Unit tests cover "
          "Aberdeen, Cardiff and Penzance.",
          tools=[T("bash", "pnpm vitest run lib/frost", "✓ lib/frost.test.ts (3 tests)")]),
    ], summary=S("Show last frost date per postcode.", "Bundled station averages and nearest-station lookup; UK first.")),

    conv("gp-04", "claude_code", "claude-haiku-4-5", "2026-11-08T10:10:00", R,
         "Dark mode for the planner", branch="feat/dark-mode", turns=[
        U("Add a dark theme that follows the system setting."),
        A("Moved the palette to CSS variables on `:root` with a `prefers-color-scheme: dark` override, and replaced "
          "the 23 hard-coded hex colours in components. The soil and plant illustrations use `currentColor`, so "
          f"they adapt too. PR: {GH}pull/42",
          tools=[T("Bash", "pnpm lint && pnpm build", "✓ Compiled successfully")]),
    ], summary=S("No dark theme.", "CSS variables with a prefers-color-scheme override.")),
]
