import json
import os
import time

CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)


def cache_path(key):
    return f"{CACHE_DIR}/{key}.json"


def get_cache(key, ttl=600):
    path = cache_path(key)

    if not os.path.exists(path):
        return None

    age = time.time() - os.path.getmtime(path)

    if age > ttl:
        return None

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def set_cache(key, data):
    path = cache_path(key)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)