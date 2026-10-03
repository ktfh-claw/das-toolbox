"""Narrow, read-only adapter around the pinned DAS pattern query client."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

from hyperon_das.service_clients import PatternMatchingQueryProxy
from hyperon_das.service_bus.service_bus import ServiceBusSingleton


class QueryFailure(RuntimeError):
    pass


class DasQueryRunner:
    """Run one bounded pattern query at a time over the callback-based DAS bus."""

    def __init__(self) -> None:
        client_endpoint = os.environ["DAS_CLIENT_ENDPOINT"]
        lower = int(os.environ["DAS_CALLBACK_PORT_LOWER"])
        upper = int(os.environ["DAS_CALLBACK_PORT_UPPER"])
        query_engine = os.environ["DAS_QUERY_ENGINE"]
        self._bus = ServiceBusSingleton(
            host_id=client_endpoint,
            known_peer=query_engine,
            port_lower=lower,
            port_upper=upper,
        ).get_instance()
        self._lock = threading.Lock()

    def query(self, tokens: list[str], max_answers: int, timeout: float) -> list[dict[str, Any]]:
        proxy = PatternMatchingQueryProxy(tokens=tokens)
        # These values are fixed here, not copied from the HTTP request. The only
        # permitted operation is pattern matching, without attention updates.
        proxy.set_parameter("attention_update_flag", False)
        proxy.set_parameter("positive_importance_flag", False)
        proxy.set_parameter("count_flag", False)
        proxy.set_parameter("unique_assignment_flag", False)
        proxy.set_parameter("use_link_template_cache", False)
        proxy.set_parameter("populate_metta_mapping", False)
        proxy.set_parameter("user_metta_as_query_tokens", False)
        proxy.set_parameter("max_bundle_size", min(max_answers, 100))
        proxy.set_parameter("max_answers", max_answers)

        answers: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout
        with self._lock:
            try:
                self._bus.issue_bus_command(proxy)
                while not proxy.finished():
                    if time.monotonic() >= deadline:
                        _best_effort_abort(proxy)
                        raise QueryFailure("query timed out")
                    answer = proxy.pop()
                    if answer is None:
                        time.sleep(0.01)
                        continue
                    answers.append(_normalize_answer(answer))
                    if len(answers) >= max_answers:
                        _best_effort_abort(proxy)
                        break
            except QueryFailure:
                raise
            except Exception as exc:
                raise QueryFailure("DAS query failed") from exc
            finally:
                try:
                    proxy.graceful_shutdown()
                except Exception:
                    pass
        return answers


def _normalize_answer(answer: Any) -> dict[str, Any]:
    assignments = dict(getattr(answer.assignment, "_mapping", {}))
    return {
        "handles": [_bounded(value) for value in answer.handles[:64]],
        "assignments": {_bounded(key): _bounded(value) for key, value in list(assignments.items())[:64]},
        "strength": float(answer.strength),
        "importance": float(answer.importance),
    }


def _bounded(value: Any) -> str:
    return str(value)[:1024]


def _best_effort_abort(proxy: Any) -> None:
    # The pinned client's abort() passes a dict to an API that appends to a
    # list. Call the same read-query control command with the expected type.
    try:
        proxy.to_remote_peer("abort", [])
    except Exception:
        pass
