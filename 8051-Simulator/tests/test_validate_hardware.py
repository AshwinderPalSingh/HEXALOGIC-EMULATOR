from validate_hardware import render_markdown_report, run_validation


def test_validation_runner_produces_reproducible_report():
    report = run_validation(clock_hz_8051=12_000_000)

    assert report["timing_accuracy"]["loop_blink"]["verdict"] == "PASS"
    assert report["timing_accuracy"]["fast_toggle"]["verdict"] == "PASS"
    assert report["timing_accuracy"]["timer_poll"]["verdict"] == "PASS"
    assert report["timing_accuracy"]["timer_irq"]["verdict"] == "PASS"
    assert report["hardware_validation"]["LED"]["status"] == "PASS"
    assert report["hardware_validation"]["GPIO"]["status"] == "PASS"
    assert report["hardware_validation"]["Timer"]["status"] == "PASS"
    assert report["hardware_validation"]["Interrupt"]["status"] == "PASS"
    assert report["hardware_validation"]["Serial RX"]["status"] == "PASS"
    assert report["hardware_validation"]["External Interrupt"]["status"] == "PASS"
    assert report["measured_behavior"]["built_in_suites"]["8051"]["passed"] is True
    assert report["measured_behavior"]["built_in_suites"]["arm"]["passed"] is True
    assert report["final_verdict"]["accuracy_score"] >= 90

    markdown = render_markdown_report(report)
    assert "## 🧾 HARDWARE EXECUTION VALIDATION REPORT" in markdown
    assert "### 10. Final Verdict" in markdown
