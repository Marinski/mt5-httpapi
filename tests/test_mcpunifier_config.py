"""The unifier's per-terminal chartctl flag, read from config.yaml.

list_terminals reports it so an agent knows which terminals serve the Chart
Deployments tools, so it has to agree with what mt5api itself decides for the
same config.yaml. These tests compare it against mt5api's own functions
(match_terminal_config, normalize_mode, chartctl_enabled) rather than a copy
of the rule.
"""
import pytest
import yaml

from mcpunifier.config import load_settings
from mcpunifier.errors import ConfigError
from mt5api import config as api_config


def _write_config(tmp_path, chartctl_block, terminal_extra):
    terminal = {"broker": "ftmo", "account": "live1", "port": 6545, **terminal_extra}
    config = {"terminals": [terminal]}
    if chartctl_block is not None:
        config["chartctl"] = chartctl_block
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path, config


def _api_decision(config):
    """CHARTCTL_ENABLED as mt5api computes it for the configured terminal."""
    matched = api_config.match_terminal_config(
        config["terminals"],
        broker="ftmo",
        account="live1",
    )
    return api_config.chartctl_enabled(
        api_config.normalize_mode(matched["mode"]),
        config.get("chartctl") or {},
        matched["chartctl"],
    )


@pytest.mark.parametrize(
    ("chartctl_block", "terminal_extra", "expected"),
    [
        (None, {"mode": "live"}, False),
        ({"enabled": False}, {"mode": "live"}, False),
        ({"enabled": True}, {"mode": "live"}, True),
        ({"enabled": True}, {"mode": "LIVE"}, True),
        # mt5api runs a terminal with no mode, or an unknown one, as live.
        ({"enabled": True}, {}, True),
        ({"enabled": True}, {"mode": "paper"}, True),
        ({"enabled": True}, {"mode": "backtest"}, False),
        ({"enabled": True}, {"mode": " Backtest "}, False),
        ({"enabled": True}, {"mode": "live", "chartctl": False}, False),
        ({"enabled": True}, {"mode": "live", "chartctl": True}, True),
        ({}, {"mode": "live"}, False),
    ],
)
def test_unifier_and_api_agree_on_whether_a_terminal_runs_chartctl(
    tmp_path,
    chartctl_block,
    terminal_extra,
    expected,
):
    path, config = _write_config(tmp_path, chartctl_block, terminal_extra)

    (terminal,) = load_settings(str(path)).terminals.values()

    assert _api_decision(config) is expected
    assert terminal.chartctl is expected


def test_a_chartctl_value_that_is_not_a_mapping_is_a_config_error(tmp_path):
    path, _ = _write_config(tmp_path, True, {"mode": "live"})

    with pytest.raises(ConfigError, match="chartctl .* must be a mapping"):
        load_settings(str(path))
