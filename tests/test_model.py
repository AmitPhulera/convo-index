from convo_index.model import content_hash, make_id, split_id, Message, Conversation, KIND_TEXT
from convo_index.lock import index_lock, IndexLocked


def test_content_hash_depends_on_role_kind_text():
    a = content_hash("user", KIND_TEXT, "hi")
    assert a == content_hash("user", KIND_TEXT, "hi")
    assert a != content_hash("assistant", KIND_TEXT, "hi")
    assert a != content_hash("user", "thinking", "hi")
    assert a != content_hash("user", KIND_TEXT, "hi!")


def test_ids_roundtrip():
    assert make_id("opencode", "ses_1") == "opencode:ses_1"
    assert split_id("claude_code:a:b") == ("claude_code", "a:b")


def test_conversation_id_and_parent():
    c = Conversation(source="opencode", native_id="k", title="t", first_prompt="p", messages=[],
                     source_ref="x", relation=("subagent", "opencode:parent"))
    assert c.id == "opencode:k" and c.parent_id == "opencode:parent"
    assert Message(seq=0, role="user", text="x").content_hash == content_hash("user", KIND_TEXT, "x")


def test_index_lock_is_exclusive(tmp_path):
    p = tmp_path / "db"
    with index_lock(p):
        try:
            with index_lock(p):
                raise AssertionError("second lock should fail")
        except IndexLocked:
            pass
    with index_lock(p):  # released properly
        pass
