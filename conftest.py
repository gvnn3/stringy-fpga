"""Repo-root pytest conftest: put the repo root on sys.path so that
``.venv-pyro/bin/pytest tests/...`` works the same as
``.venv-pyro/bin/python3 -m pytest`` (``pyro`` is not pip-installed in the
venv; the ``-m`` form only worked via cwd insertion)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
