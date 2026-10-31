"""vLLM log filter: keeps user prompts and model outputs out of stdout/stderr.

It is attached through VLLM_LOGGING_CONFIG_PATH (see logging_config.json) as a
filter on the handler of the `vllm` logger, so it sees every vLLM record,
including RequestLogger, where vLLM writes "Received request <id>: ..." and
"Generated response <id>: output: ...". Its module moved between releases
(`vllm.entrypoints.logger` in older ones, `vllm.entrypoints.serve.utils.request_logger`
in 0.27), so it is matched by module name, not by the full path.

Approach:
  1. Records from RequestLogger are replaced entirely with a safe line: the event
     (received/completed) plus the request id. The fact of a request to the model
     stays in the log.
  2. As a safety net for all other vLLM records, everything following the markers
     prompt / prompt_token_ids / output / output_token_ids / messages
     is replaced with [REDACTED].
We do NOT rely on the exact vLLM message format: any unrecognized message from
RequestLogger is anonymized as well.
"""
from __future__ import annotations

import logging
import re

REDACTED = "[REDACTED]"

# loggers that, by definition, write user content (RequestLogger in any vLLM release)
_CONTENT_LOGGERS = ("vllm.entrypoints.logger", "vllm.entrypoints.serve.utils.request_logger")
_CONTENT_MODULES = ("request_logger",)

_REQ_ID = re.compile(r"\b(?:request|response)\s+([A-Za-z0-9][\w\-\.]*)", re.IGNORECASE)

# content marker -> up to the end of the value (up to the next ", <next_key>:" or end of string)
_CONTENT_KEYS = r"(?:prompt_token_ids|prompt_embeds|prompt|output_token_ids|output|messages|text)"
_NEXT_KEY = r"(?:params|lora_request|prompt_embeds_shape|output_token_ids|prompt_token_ids|prompt|output|finish_reason|stop_reason)"
_SCRUB = re.compile(
    rf"(\b{_CONTENT_KEYS}\b\s*[:=]\s*).*?(?=,\s*{_NEXT_KEY}\s*[:=]|$)",
    re.IGNORECASE | re.DOTALL,
)


def _is_content_logger(name: str) -> bool:
    return name.startswith(_CONTENT_LOGGERS) or name.rsplit(".", 1)[-1] in _CONTENT_MODULES


def _event(message: str) -> str:
    low = message.lstrip().lower()
    if low.startswith("received"):
        return "received"
    if low.startswith(("generated", "finished", "completed")):
        return "completed"
    if low.startswith(("aborted", "abort")):
        return "aborted"
    return "event"


class PromptRedactFilter(logging.Filter):
    """logging.Filter that anonymizes vLLM records. Never breaks logging."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            message = record.getMessage()
            if _is_content_logger(record.name):
                m = _REQ_ID.search(message)
                req_id = m.group(1).rstrip(":") if m else "unknown"
                record.msg = "model request %s: id=%s (prompt/output not logged)"
                record.args = (_event(message), req_id)
            else:
                scrubbed = _SCRUB.sub(lambda m: m.group(1) + REDACTED, message)
                if scrubbed != message:
                    record.msg, record.args = scrubbed, ()
        except Exception:  # noqa: BLE001 - logging must not crash the server,
            # but must not leak either: when in doubt, hide the whole message
            record.msg, record.args = "log record redacted (filter error)", ()
        return True
