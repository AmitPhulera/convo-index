"""infra-playbooks: fictional Terraform + Ansible for acme-labs (work). Tickets OPS-*."""
from demo.base import A, S, T, U, conv

R = "infra-playbooks"
PR = "https://github.com/acme-labs/infra-playbooks/pull/"

CONVERSATIONS = [
    conv("ops-01", "claude_code", "claude-sonnet-4-6", "2026-09-08T08:30:00", R,
         "Terraform plan wants to recreate the database", branch="fix/OPS-41-rds-replace", turns=[
        U("OPS-41: `terraform plan` says it will destroy and recreate the production Postgres instance. "
          "Nobody changed it. Help!"),
        A("Don't apply. Let me see which attribute forces replacement.",
          tools=[T("Bash", "terraform plan -target=module.db -no-color | grep -B2 -A2 'forces replacement'",
                   "      ~ engine_version = \"16.3\" -> \"16\" # forces replacement")]),
        A("`engine_version` went from `16.3` (what AWS reports after an automatic minor upgrade) to `16` "
          "(what our module pins). Terraform reads that as a change and this provider version treats it as "
          "replace-only.\n\nSafe fix: set `engine_version = \"16\"` plus `auto_minor_version_upgrade = true` and add "
          "`lifecycle { ignore_changes = [engine_version] }`, then also add `prevent_destroy = true` to the "
          "instance so a plan like this fails loudly next time.",
          tools=[T("Edit", {"file_path": "modules/db/main.tf", "old_string": "  lifecycle {",
                            "new_string": "  lifecycle {\n    prevent_destroy = true\n    ignore_changes  = [engine_version]"}),
                 T("Bash", "terraform plan -target=module.db -no-color | tail -1",
                   "No changes. Your infrastructure matches the configuration.")]),
    ], summary=S("Terraform planned to replace the production database (OPS-41).",
                 "An AWS minor-version upgrade drifted engine_version; ignore_changes plus prevent_destroy.",
                 "Put prevent_destroy on stateful resources.")),

    conv("ops-02", "codex", "gpt-5.6-terra", "2026-09-19T10:00:00", R,
         "Rotate the TLS certificates with Ansible", branch="feat/OPS-47-cert-rotation", turns=[
        U("OPS-47: write a playbook that rotates the internal TLS certs on the haproxy hosts without downtime."),
        A("Plan: issue new certs from the internal CA, push them next to the old ones, validate with "
          "`haproxy -c`, then `systemctl reload` one host at a time (`serial: 1`) and check the health endpoint "
          "before moving on.",
          tools=[T("shell", "ansible-playbook playbooks/rotate-certs.yml --check --diff -l haproxy-staging",
                   "PLAY RECAP\nhaproxy-staging-1 : ok=9 changed=3 unreachable=0 failed=0\nhaproxy-staging-2 : ok=9 changed=3 unreachable=0 failed=0")]),
        A("Dry run on staging is clean. The playbook keeps the previous cert as `*.pem.prev` and has a "
          "`rollback` tag that swaps them back."),
    ], summary=S("Zero-downtime TLS certificate rotation for haproxy (OPS-47).",
                 "Rolling Ansible playbook: issue, validate, reload one host at a time, with a rollback tag.")),

    conv("ops-03", "antigravity", "", "2026-10-09T15:45:00", R,
         "Why is the staging bill so high?", turns=[
        U("Staging costs went from $900 to $3,100 last month. Find out what changed."),
        A("Grouping the cost report by resource tag.",
          tools=[T("run_command", "aws ce get-cost-and-usage --time-period Start=2026-09-01,End=2026-10-01 --granularity MONTHLY --group-by Type=TAG,Key=service",
                   "service$search  1980.40\nservice$web  412.10\nservice$db  388.00")]),
        A("Search accounts for ~$2k. The OpenSearch domain was resized to `r6g.2xlarge.search` x3 during a load "
          "test on 2026-09-04 and never scaled back. Scaling it to `t3.medium.search` x1 like before would "
          "bring staging back to roughly $950/month. I didn't change anything; it's in "
          "`envs/staging/search.tf` if you want me to."),
    ], summary=S("Staging cost tripled in a month.",
                 "A load-test resize of the OpenSearch domain was never reverted; proposed scaling it back.")),

    conv("ops-04", "claude_code", "claude-sonnet-4-6", "2026-10-28T09:20:00", R,
         "Add a GitHub Actions job to lint playbooks", branch="ci/ansible-lint", turns=[
        U("Add CI that runs ansible-lint and terraform fmt/validate on every PR."),
        A("Added `.github/workflows/lint.yml` with two jobs: `ansible-lint` (pinned to 25.x, production profile) "
          "and `terraform` (`fmt -check -recursive`, then `init -backend=false && validate` per env). "
          "Fixed the 11 existing lint findings in a separate commit so the workflow lands green.",
          tools=[T("Bash", "ansible-lint --profile production", "Passed: 0 failure(s), 0 warning(s) on 64 files.")]),
        A(f"PR: {PR}58"),
    ], summary=S("No CI for infra code.", "ansible-lint and terraform fmt/validate on every PR; existing findings fixed first.")),
]
