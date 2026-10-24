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


# ---- layered settings -----------------------------------------------------------------
# Highest priority first (a key set in an earlier layer wins):
#   1. real environment variables
#   2. cache/settings_override.env  set from Discord with /set (this server only)
#   3. settings.env                 in the GitHub repo, so settings can be changed remotely
#                                   by pushing; never holds secrets (they're skipped)
#   4. .env                         the server's own file: keys, tokens, everything else
import re

SECRET = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|WEBHOOK", re.I)


def root_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def override_path(root: str = None) -> str:
    return os.path.join(root or root_dir(), "cache", "settings_override.env")


def read_env(path: str) -> dict:
    out = {}
    if not os.path.exists(path):
        return out
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
            if key and key not in out:     # first one wins, like load_env
                out[key] = value
    return out


def load_all(root: str = None) -> None:
    root = root or root_dir()
    load_env(override_path(root))
    for key, value in read_env(os.path.join(root, "settings.env")).items():
        if value and not SECRET.search(key) and key not in os.environ:
            os.environ[key] = value
    load_env(os.path.join(root, ".env"))


def layers(root: str = None) -> list:
    """[(source name, {key: value})], highest priority first."""
    root = root or root_dir()
    repo = {k: v for k, v in read_env(os.path.join(root, "settings.env")).items() if not SECRET.search(k)}
    return [("set from Discord", read_env(override_path(root))), ("settings.env (GitHub)", repo),
            (".env", read_env(os.path.join(root, ".env")))]


def effective(root: str = None) -> dict:
    """key -> (value, source) for every key any layer sets (empty values included)."""
    out = {}
    for source, values in layers(root):
        for k, v in values.items():
            if k not in out and v != "":
                out[k] = (v, source)
    for source, values in layers(root):
        for k, v in values.items():
            out.setdefault(k, (v, source))
    return out


def mask(key: str, value: str) -> str:
    if SECRET.search(key) and value:
        return f"set (…{value[-4:]})" if len(value) > 8 else "set"
    return value if value != "" else "(blank)"


def set_override(key: str, value: str, root: str = None) -> None:
    path = override_path(root)
    values = read_env(path)
    values[key] = value
    _write(path, values)


def remove_override(key: str, root: str = None) -> bool:
    path = override_path(root)
    values = read_env(path)
    if key not in values:
        return False
    del values[key]
    _write(path, values)
    return True


def _write(path: str, values: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        f.write("# Set from Discord with /set. Wins over settings.env and .env. /unset removes.\n")
        for k, v in values.items():
            f.write(f"{k}={v}\n")
    os.replace(path + ".tmp", path)
