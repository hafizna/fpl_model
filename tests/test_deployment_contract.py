from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_vercel_function_has_a_bounded_runtime_and_keeps_the_compact_release():
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))

    function = config["functions"]["api/**/*.py"]
    assert function["maxDuration"] == 30
    assert "tests/**" in function["excludeFiles"]
    assert "data/**" in function["excludeFiles"]
    assert "web/**" not in function["excludeFiles"]
    assert (ROOT / "web" / "release.json").is_file()


def test_vercel_python_version_is_explicit_and_supported():
    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.13"


def test_environment_example_contains_no_secret_value():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "FPL_WEB_HOST=127.0.0.1" in example
    assert "FPL_WEB_PORT=8000" in example
    assert "FPL_EXPOSE_API_DOCS=true" in example
    assert "FPL_ALLOWED_RELEASE_HEALTH=research,shadow,production" in example
    assert "FPL_TRANSFER_SCAN_ENABLED=true" in example
    assert "FPL_REQUIRE_ALPHA_ACCESS=false" in example
    assert "FPL_LEGAL_NOTICE_REVIEWED=false" in example
    assert "FPL_ALPHA_ACCESS_TOKEN_HASHES=<tester-label>=<sha256>" in example
    assert "FPL_ALPHA_ACCESS_TOKEN=" not in example
    assert "CRON_SECRET=" not in example
