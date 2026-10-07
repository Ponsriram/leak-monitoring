"""The crawl settings must be the same in three places: the code, .env.example, compose.

A default that drifts between them is a deployment that quietly behaves differently from the
documentation. This reads all three and compares, so adding or changing a setting in one place
without the others fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

from intel.config import Settings

REPO = Path(__file__).resolve().parents[3]


def code_defaults() -> dict[str, str]:
    out: dict[str, str] = {}
    for field in Settings.model_fields.values():
        alias = field.alias
        if alias and alias.startswith("CRAWL_"):
            default = field.default
            out[alias] = str(default).lower() if isinstance(default, bool) else str(default)
    return out


def env_example() -> dict[str, str]:
    pairs = re.findall(
        r"^(CRAWL_[A-Z0-9_]+)=(.*)$", (REPO / ".env.example").read_text("utf-8"), re.M
    )
    return dict(pairs)


def compose_worker() -> dict[str, str]:
    text = (REPO / "infra" / "docker-compose.yml").read_text("utf-8")
    pairs = re.findall(r"^\s+(CRAWL_[A-Z0-9_]+): \$\{[A-Z0-9_]+:-([^}]*)\}$", text, re.M)
    return dict(pairs)


def test_every_crawl_setting_is_documented_and_wired() -> None:
    code, example, compose = code_defaults(), env_example(), compose_worker()
    assert set(example) == set(code), "settings missing from .env.example (or extra in it)"
    assert set(compose) == set(code), "settings missing from docker-compose.yml (or extra in it)"


def test_the_defaults_agree_everywhere() -> None:
    code, example, compose = code_defaults(), env_example(), compose_worker()
    for name, default in code.items():
        assert example[name] == default, (
            f"{name}: .env.example says {example[name]}, code {default}"
        )
        assert compose[name] == default, f"{name}: compose says {compose[name]}, code {default}"


def test_the_timeout_is_still_sixty_seconds() -> None:
    assert code_defaults()["CRAWL_TIMEOUT"] == "60"
