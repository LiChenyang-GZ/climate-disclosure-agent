from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from alembic.config import Config


def _alembic_config(pg_url):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", pg_url.replace("%", "%%"))
    return config


spec = spec_from_file_location(
    "compare_run_helpers", Path(__file__).resolve().parents[1] / "agent/test_run.py"
)
assert spec is not None and spec.loader is not None
helpers = module_from_spec(spec)
spec.loader.exec_module(helpers)
run_env = helpers.run_env


@pytest.fixture
def run_helpers():
    return helpers
