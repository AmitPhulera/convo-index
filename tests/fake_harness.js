// The fake harness for Windows CI, where the Claude Code launcher runs `node <cli.js>` instead
// of an npm `.cmd` shim. Same FAKE_HARNESS steps as fake_harness.py (the subset the server
// tests use): line, sleep, exit, echo_stdin, stdin_to, echo_env, stderr.
const fs = require("fs");
const steps = JSON.parse(process.env.FAKE_HARNESS || "[]");
const sleep = (s) => { const end = Date.now() + s * 1000; while (Date.now() < end) { Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 50); } };
let stdin = "";
try { stdin = fs.readFileSync(0, "utf8"); } catch (e) { stdin = ""; }
for (const step of steps) {
  if ("line" in step) fs.writeSync(1, step.line + "\n");
  else if ("sleep" in step) sleep(step.sleep);
  else if ("stderr" in step) fs.writeSync(2, "e".repeat(step.stderr));
  else if (step.echo_stdin) fs.writeSync(1, JSON.stringify(stdin) + "\n");
  else if ("stdin_to" in step) fs.writeFileSync(step.stdin_to, stdin);
  else if ("argv_to" in step) fs.writeFileSync(step.argv_to, process.argv.slice(2).join("\n"));
  else if ("echo_env" in step) fs.writeSync(1, JSON.stringify(process.env[step.echo_env] ?? null) + "\n");
  else if ("exit" in step) process.exit(step.exit);
}
