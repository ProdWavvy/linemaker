"""Load KEY=value lines from a .env file into os.environ (existing variables win)."""
import os


def load_env(path: str = ".env") -> list:
    loaded = []
    if not os.path.exists(path):
        return loaded
    with open(path, encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and value and key not in os.environ:
                os.environ[key] = value
                loaded.append(key)
    return loaded
