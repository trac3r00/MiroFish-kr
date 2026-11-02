import random
import threading
from types import SimpleNamespace

from openai.types.chat import ChatCompletion

from app.llm.usage import UsageTracker, usage_from_chat_completion

SEED = 4242
CASES = 300


def test_record_aggregates_by_stage_profile_model():
    tracker = UsageTracker()
    tracker.record("report", "default", "m", input_tokens=10, cached_tokens=4, output_tokens=3)
    tracker.record("report", "default", "m", input_tokens=5, output_tokens=1)
    tracker.record("ontology", "boost", "b", input_tokens=1)

    snapshot = tracker.snapshot()
    assert snapshot["entries"] == [
        {"stage": "ontology", "profile_id": "boost", "model": "b", "calls": 1, "input_tokens": 1, "cached_tokens": 0, "output_tokens": 0},
        {"stage": "report", "profile_id": "default", "model": "m", "calls": 2, "input_tokens": 15, "cached_tokens": 4, "output_tokens": 4},
    ]
    assert snapshot["totals"] == {"calls": 3, "input_tokens": 16, "cached_tokens": 4, "output_tokens": 4}
    tracker.reset()
    assert tracker.snapshot()["entries"] == []


def test_record_is_thread_safe():
    tracker = UsageTracker()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        for _ in range(500):
            tracker.record("profile", "default", "m", input_tokens=2, cached_tokens=1, output_tokens=3)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert tracker.snapshot()["totals"] == {"calls": 4000, "input_tokens": 8000, "cached_tokens": 4000, "output_tokens": 12000}


def test_property_snapshot_matches_reference_model():
    rng = random.Random(SEED)
    executed = 0
    for _ in range(CASES):
        tracker = UsageTracker()
        model = {}
        for _ in range(rng.randint(0, 40)):
            key = (rng.choice(["report", "profile", "config"]), rng.choice(["default", "boost"]), rng.choice(["m1", "m2"]))
            tokens = [rng.randint(0, 5000) for _ in range(3)]
            tracker.record(*key, *tokens)
            row = model.setdefault(key, [0, 0, 0, 0])
            row[0] += 1
            for i in range(3):
                row[i + 1] += tokens[i]

        snapshot = tracker.snapshot()
        got = {(e["stage"], e["profile_id"], e["model"]): [e["calls"], e["input_tokens"], e["cached_tokens"], e["output_tokens"]] for e in snapshot["entries"]}
        assert got == model
        assert snapshot["totals"]["calls"] == sum(row[0] for row in model.values())
        executed += 1
    assert executed == CASES


def _completion(usage):
    return ChatCompletion.model_validate({
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}],
        "usage": usage,
    })


def test_usage_from_openai_cached_tokens():
    response = _completion({"prompt_tokens": 100, "completion_tokens": 7, "total_tokens": 107, "prompt_tokens_details": {"cached_tokens": 64}})
    assert usage_from_chat_completion(response) == {"input_tokens": 100, "cached_tokens": 64, "output_tokens": 7}


def test_usage_from_deepseek_cache_hit_tokens():
    response = _completion({"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55, "prompt_cache_hit_tokens": 32, "prompt_cache_miss_tokens": 18})
    assert usage_from_chat_completion(response) == {"input_tokens": 50, "cached_tokens": 32, "output_tokens": 5}


def test_usage_missing_is_zero():
    assert usage_from_chat_completion(SimpleNamespace(choices=[])) == {"input_tokens": 0, "cached_tokens": 0, "output_tokens": 0}
