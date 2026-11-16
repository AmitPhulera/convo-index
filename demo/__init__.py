"""Fictional conversations for the public playground (see scripts/make_demo_data.py)."""
from demo import acme_shop, garden_planner, infra_playbooks, ledgerly, tidewatch

CONVERSATIONS = [c for m in (acme_shop, ledgerly, infra_playbooks, tidewatch, garden_planner) for c in m.CONVERSATIONS]
