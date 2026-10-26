from convo_index.classify import extract_tickets


def test_extract_tickets_dedup_sorted(settings):
    assert extract_tickets("Saw SAAS-42 and PR for SAAS-42, also QA-7 done", settings) == ["QA-7", "SAAS-42"]


def test_extract_tickets_none(settings):
    assert extract_tickets("no tickets here", settings) == []


def test_extract_tickets_ignores_encodings_and_hashes(settings):
    assert extract_tickets("Encoded as UTF-8 with SHA-256 hash, fixing SAAS-99 on EC2-2 box", settings) == ["SAAS-99"]


def test_extract_tickets_regex_is_configurable(settings):
    settings["references"]["ticket_regex"] = r"\bGH-\d+\b"
    assert extract_tickets("GH-12 and SAAS-1", settings) == ["GH-12"]


def test_extract_tickets_with_capturing_groups_in_user_regex(settings):
    settings["references"]["ticket_regex"] = r"\b([A-Z]{2,})-(\d+)\b"
    assert extract_tickets("see SAAS-1 and QA-22", settings) == ["QA-22", "SAAS-1"]
