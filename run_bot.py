"""Start the Linemaker Discord bot. In Eclipse/PyDev: right-click this file > Run As > Python Run.

Reads .env from this folder, so it works whatever working directory the IDE uses.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from linemaker.envfile import load_all  # noqa: E402

load_all(HERE)   # Discord /set overrides, then settings.env (GitHub), then .env

from linemaker.bot import main  # noqa: E402

if __name__ == "__main__":
    main()
