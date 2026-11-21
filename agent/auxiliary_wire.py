"""Message hygiene at the resolved auxiliary client boundary."""

from agent.transports.chat_completions import ChatCompletionsTransport


def prepare_chat_messages(client, kwargs: dict) -> dict:
    """Sanitize actual Chat Completions SDK requests, not native adapter replay.

    Auxiliary and MoA callers can retain a prepared request before the virtual
    transport sanitizes its copy. The resolved SDK client identifies the wire;
    native Messages/Responses adapters must retain their reasoning sidecars.
    """
    # The openai classes load lazily, through the one locked loader in
    # auxiliary_client — importing them at module level dragged the whole
    # openai package (and openai.types) in the moment the title thread
    # imported this wire, racing the turn thread's provider/ACP imports
    # into an import deadlock (_ModuleLock('openai.types')).
    from agent.auxiliary_client import _load_async_openai_cls, _load_openai_cls

    if not isinstance(client, (_load_openai_cls(), _load_async_openai_cls())) or "messages" not in kwargs:
        return kwargs
    from agent.reasoning_carriers import shape_wire_carriers

    base_url = str(getattr(client, "base_url", "") or "")
    messages = ChatCompletionsTransport().convert_messages(
        shape_wire_carriers(kwargs["messages"], model=kwargs.get("model"), base_url=base_url),
        model=kwargs.get("model"), base_url=base_url,
    )
    return {**kwargs, "messages": messages}


def relay_chat_metadata(metadata: dict) -> dict:
    """Relay metadata for an auxiliary attempt.

    Relay intercepts ``client.chat.completions.create``, so it decodes a chat-completions
    request even when the adapter speaks Responses or Messages to its provider.
    """
    return {**metadata, "api_mode": "chat_completions"}
