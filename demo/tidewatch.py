"""tidewatch: a fictional Rust CLI that predicts tides (personal). Issues on tidewatch-rs/tidewatch."""
from demo.base import A, S, T, U, conv

R = "tidewatch"
GH = "https://github.com/tidewatch-rs/tidewatch/"

CONVERSATIONS = [
    conv("tide-01", "claude_code", "claude-opus-4-8", "2026-09-06T20:10:00", R,
         "Harmonic tide prediction is off by an hour", turns=[
        U("tidewatch predicts high tide in Lisbon an hour later than the official tables. Here's the output:\n"
          "`tidewatch predict --station lisbon --date 2026-09-07` → 14:52, official 13:51."),
        A("An exact one-hour offset smells like a timezone/DST issue rather than the harmonics.",
          thinking="Lisbon is UTC+1 in summer. If constituents are computed in UTC and then formatted as local without conversion, or converted twice, we'd be off by exactly 1h.",
          tools=[T("Grep", {"pattern": "with_timezone|Local|FixedOffset", "path": "src"},
                   "src/output.rs:22:    let local = t.with_timezone(&FixedOffset::east_opt(station.utc_offset * 3600).unwrap());")]),
        A("`output.rs` converts with the station's *standard* offset from the station file (`utc_offset = 0` for "
          "Lisbon), ignoring daylight saving. I switched the station file to an IANA zone (`Europe/Lisbon`) and use "
          "`chrono-tz` for the conversion.",
          tools=[T("Bash", "cargo test && cargo run -- predict --station lisbon --date 2026-09-07",
                   "test result: ok. 58 passed; 0 failed\nHigh  13:51  3.42 m\nLow   20:04  0.81 m")]),
    ], summary=S("Predictions were an hour late in summer.",
                 "Stations stored a fixed UTC offset; switched to IANA zones with chrono-tz.",
                 "Store timezones, not offsets.")),

    conv("tide-02", "pi", "claude-sonnet-4-6", "2026-09-27T21:00:00", R,
         "Speed up the constituent calculation", turns=[
        U("Predicting a full year takes 9 seconds. Can we make it faster?"),
        A("", tools=[T("bash", "cargo build --release && hyperfine 'target/release/tidewatch predict --station brest --year 2026'",
                       "Time (mean ± σ):      9.112 s ±  0.081 s")]),
        A("Profiling shows 80% of the time in `node_factors()`, which is recomputed for every minute but only "
          "changes per day. Caching it per day and evaluating the 37 constituents with precomputed angular "
          "speeds brings a year down to 0.21s. Output is bit-identical on the 12 reference stations.",
          tools=[T("bash", "hyperfine 'target/release/tidewatch predict --station brest --year 2026'",
                   "Time (mean ± σ):     212.4 ms ±   3.1 ms")]),
    ], summary=S("A year of predictions took 9s.", "Cache node factors per day; 43x faster with identical output.")),

    conv("tide-03", "codex", "gpt-5.5", "2026-10-11T19:30:00", R,
         "Publish to crates.io with cargo-release", turns=[
        U("Set up releases so I can publish tidewatch to crates.io with one command."),
        A("Added `release.toml` for cargo-release (signed tags, `CHANGELOG.md` updated from the Unreleased section) "
          "and a GitHub workflow that builds binaries for linux/mac/windows when a `v*` tag is pushed.",
          tools=[T("shell", "cargo release patch --no-publish --no-push", "Upgrading tidewatch from 0.4.1 to 0.4.2\n[dry run] tag v0.4.2")]),
    ], summary=S("Manual releases.", "cargo-release config and a tag-triggered binary build workflow.")),

    conv("tide-04", "opencode", "claude-sonnet-4-5", "2026-10-25T10:00:00", R,
         "Support NOAA station files", turns=[
        U(f"Someone asked in {GH}issues/17 for NOAA harmonic constituent files. Add a parser."),
        A("NOAA publishes constituents as JSON from their metadata API (amplitude in feet, phase in degrees GMT). "
          "I added `stations::noaa::parse`, converting feet to metres and mapping NOAA names (`M2`, `S2`, `K1`, …) "
          "onto our constituent table; unknown ones are skipped with a warning.",
          tools=[T("bash", "cargo test noaa", "running 6 tests\ntest stations::noaa::tests::parses_seattle ... ok\ntest result: ok. 6 passed")]),
        A(f"Opened {GH}pull/18, which closes #17."),
    ], summary=S("Users wanted NOAA station data (issue #17).", "NOAA JSON parser with unit conversion and name mapping.")),
]
