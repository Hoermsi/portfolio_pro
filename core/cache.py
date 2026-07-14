"""Einfacher TTL-Cache-Decorator, framework-unabhängig (kein Streamlit nötig)."""
import functools
import threading
import time


def ttl_cache(seconds: float):
    """Cached Funktionsergebnisse für `seconds`. Key = (args, kwargs)."""

    def decorator(fn):
        store: dict = {}
        lock = threading.Lock()

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            with lock:
                hit = store.get(key)
                if hit and now - hit[0] < seconds:
                    return hit[1]
            result = fn(*args, **kwargs)
            with lock:
                store[key] = (now, result)
                # simple Größenbegrenzung
                if len(store) > 512:
                    oldest = sorted(store.items(), key=lambda kv: kv[1][0])[:256]
                    for k, _ in oldest:
                        store.pop(k, None)
            return result

        wrapper.cache_clear = store.clear  # type: ignore[attr-defined]
        return wrapper

    return decorator
