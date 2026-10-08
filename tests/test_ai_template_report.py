from core.replay.ai_report import render_ai_template_report


def test_partial_report_does_not_invent_win_rate_or_hide_errors(tmp_path):
    report = {"status": "PAUSED", "complete_window": False, "comparison_eligible": False,
              "errors": [{"template_id": "s1", "error": "INVALID_SCHEMA"}],
              "results": [{"template_id": "s1", "name": "<script>unsafe()</script>",
                           "roi": 0, "closed_trade_count": 0, "win_rate": None}]}
    output = render_ai_template_report(report, tmp_path / "report.html")
    page = output.read_text(encoding="utf-8")
    assert "无已平仓样本" in page and "中途快照" in page
    assert "INVALID_SCHEMA" in page
    assert "<script>unsafe()" not in page


def test_complete_report_separates_roi_and_net_closed_win_rate(tmp_path):
    report = {"status": "COMPLETED", "complete_window": True, "comparison_eligible": True,
              "common_initial_equity_usdt": 1000,
              "results": [{"template_id": "s1", "roi": 0.035, "closed_trade_count": 4,
                           "wins": 3, "win_rate": 0.75, "max_drawdown": 0.02, "ending_equity": 1035}]}
    page = render_ai_template_report(report, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "+3.50%" in page and "75.0%" in page and "-2.00%" in page
    assert "历史区间已跑完" in page
    assert "独立初始资金 1,000.00 USDT" in page and "不代表当前实盘账户收益" in page


def test_participation_separates_model_proposals_accepted_and_filled_entries(tmp_path):
    report = {"results": [{"template_id": "s1", "decision_count": 10,
        "action_counts": {"WAIT": 8, "OPEN_LONG": 2}, "accepted_entry_order_count": 2,
        "filled_entry_order_count": 1, "filled_order_count": 3}]}
    page = render_ai_template_report(report, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "2 / 20.0%" in page and "1 / 50.0%" in page
    assert "开单频率与成交转化" in page


def test_unsupported_economic_path_has_no_invented_full_return(tmp_path):
    report = {"status": "COMPLETED", "complete_window": True, "comparison_eligible": False,
        "results": [{"template_id": "s1", "economic_eligible": False,
                     "halted_reason": "UNSUPPORTED_LIQUIDATION_PATH", "roi": None,
                     "ending_equity": None, "max_drawdown": None, "win_rate": None}]}
    page = render_ai_template_report(report, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "UNSUPPORTED_LIQUIDATION_PATH" in page
    assert "尚不能作完整比较" in page
    assert "+0.00%" not in page
