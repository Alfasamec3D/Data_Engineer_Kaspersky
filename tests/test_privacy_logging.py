"""Checks the log filter without installing vLLM:  python3 tests/test_privacy_logging.py"""
import io
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "privacy_logging"))
from vllm_privacy_logging import PromptRedactFilter  # noqa: E402

SECRET_PROMPT = "my password is hunter2"
SECRET_OUTPUT = "the capital is Paris, secret-answer"


def make(name: str):
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    h.addFilter(PromptRedactFilter())
    lg = logging.getLogger(name)
    lg.handlers, lg.propagate = [h], False
    lg.setLevel(logging.DEBUG)
    return lg, buf


def test_request_logger():
    lg, buf = make("vllm.entrypoints.logger")
    lg.info(
        "Received request %s: prompt: %r, params: %s, prompt_token_ids: %s, "
        "prompt_embeds shape: %s, lora_request: %s.",
        "chatcmpl-123", SECRET_PROMPT, "SamplingParams(temp=0)", [1, 2, 3], None, None,
    )
    lg.info("Generated response %s%s: output: %r, output_token_ids: %s, finish_reason: %s",
            "chatcmpl-123", "", SECRET_OUTPUT, [4, 5], "stop")
    lg.info("totally unexpected format with %s", SECRET_PROMPT)
    out = buf.getvalue()
    assert SECRET_PROMPT not in out and "hunter2" not in out, out
    assert "secret-answer" not in out and "Paris" not in out, out
    assert "[1, 2, 3]" not in out, out
    assert out.count("chatcmpl-123") == 2, out  # the fact of the request is still logged
    assert "received" in out and "completed" in out, out


def test_request_logger_vllm_0_27():
    # vLLM 0.27 moved RequestLogger; the prompt goes to a DEBUG record "Request <id> details"
    lg, buf = make("vllm.entrypoints.serve.utils.request_logger")
    lg.debug("Request %s details: prompt: %r, prompt_token_ids: %s, prompt_embeds shape: %s.",
             "chatcmpl-777", SECRET_PROMPT, [1, 2, 3], None)
    lg.info("Received request %s: params: %s, lora_request: %s.",
            "chatcmpl-777", "SamplingParams(temp=0)", None)
    out = buf.getvalue()
    assert "hunter2" not in out and "[1, 2, 3]" not in out, out
    assert "SamplingParams" not in out, out  # the whole record is replaced
    assert out.count("model request") == 2 and out.count("chatcmpl-777") == 2, out
    assert "received" in out, out


def test_other_vllm_loggers_scrubbed():
    lg, buf = make("vllm.engine.something")
    lg.info("Added request x: prompt: %r, params: foo", SECRET_PROMPT)
    lg.info("Engine throughput: 12.3 tokens/s")  # regular logs are left untouched
    out = buf.getvalue()
    assert "hunter2" not in out, out
    assert "Engine throughput: 12.3 tokens/s" in out, out


if __name__ == "__main__":
    test_request_logger()
    test_request_logger_vllm_0_27()
    test_other_vllm_loggers_scrubbed()
    print("OK: privacy filter tests passed")
