"""A footered final reply must publish: the completion frame's deepcopy
crashed on ``FooteredReply`` (P28), losing the message.complete frame for
every turn whose reply carried the runtime footer."""

from gateway.runtime_footer import FooteredReply
from gateway.session_events import SessionEvents


def test_publish_completion_payload_with_footered_reply():
    stream = SessionEvents()
    reply = FooteredReply("hello", "gpt-5.4 · 12% · ~")
    stream.publish("session", {"text": reply, "content": reply, "admission_id": "a"})
    replay = stream.since(stream.epoch, 0)
    assert replay["count"] == 1
    payload = replay["events"][0]["payload"]
    assert payload["text"] == "hello\n\ngpt-5.4 · 12% · ~"
    assert payload["content"] == payload["text"]
    assert payload["text"].count("gpt-5.4 · 12% · ~") == 1
