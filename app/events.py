import json
from typing import Any, Literal, TypedDict


EventName = Literal["progress", "chunk", "confirm", "done", "error", "ping", "thinking"]


class StreamEvent(TypedDict):
    event: EventName
    data: dict[str, Any]


def sse_format(event: EventName, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
