import pytest

from app.common.modes import LiveTradingNotAuthorizedError, Mode, ModeGovernor, InvalidModeError


def test_research_mode_allows_nothing():
    assert not Mode.RESEARCH.allows_hypothetical_trades
    assert not Mode.RESEARCH.allows_order_submission


def test_shadow_allows_hypothetical_not_orders():
    assert Mode.SHADOW.allows_hypothetical_trades
    assert not Mode.SHADOW.allows_order_submission


def test_paper_and_live_allow_orders():
    assert Mode.PAPER.allows_order_submission
    assert Mode.LIVE.allows_order_submission


def test_default_construction_cannot_reach_live_without_both_flags():
    gov = ModeGovernor(config_mode=Mode.LIVE, cli_live_flag_present=False, local_config_path_exists=True)
    with pytest.raises(LiveTradingNotAuthorizedError):
        gov.assert_execution_allowed(Mode.LIVE)


def test_live_blocked_without_local_config_even_with_cli_flag():
    gov = ModeGovernor(config_mode=Mode.LIVE, cli_live_flag_present=True, local_config_path_exists=False)
    with pytest.raises(LiveTradingNotAuthorizedError):
        gov.assert_execution_allowed(Mode.LIVE)


def test_live_allowed_only_with_both_authorizations():
    gov = ModeGovernor(config_mode=Mode.LIVE, cli_live_flag_present=True, local_config_path_exists=True)
    gov.assert_execution_allowed(Mode.LIVE)  # should not raise


def test_cannot_request_execution_mode_different_from_configured():
    gov = ModeGovernor(config_mode=Mode.SHADOW, cli_live_flag_present=False, local_config_path_exists=False)
    with pytest.raises(InvalidModeError):
        gov.assert_execution_allowed(Mode.PAPER)


def test_shadow_and_paper_never_need_live_flags():
    gov = ModeGovernor(config_mode=Mode.PAPER, cli_live_flag_present=False, local_config_path_exists=False)
    gov.assert_execution_allowed(Mode.PAPER)  # should not raise
