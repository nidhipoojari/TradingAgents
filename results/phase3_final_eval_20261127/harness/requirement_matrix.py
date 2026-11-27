"""Map each checklist requirement to the tests that verify it, with their result in a JUnit file.

    python requirement_matrix.py JUNIT.xml [EXTRA_JUNIT.xml ...] > matrix.md
A requirement is PASS when every mapped test passed, FAIL when any failed, NOT TESTED when none was found.
"""
import re
import sys
import xml.etree.ElementTree as ET

R = {  # id: (requirement, test type, [(file regex, test-name regex)])
    "3.1-paper": ("F1 paper=True only", "unit", [
        ("test_alpaca_paper|test_order_policy", r"paper_only|only_the_paper_endpoint|repointed_at_live|enum_base_url")]),
    "3.1-cap": ("F1 maximum order notional", "unit + property", [
        ("test_alpaca_paper|test_order_policy|test_limit_orders", r"cap|notional|exceed|limit_comes_from_the_environment")]),
    "3.1-approval": ("F1 explicit human approval required", "unit + integration", [
        ("test_alpaca_paper|test_order_policy|test_limit_orders|test_dashboard_logic",
         r"approv|nothing_is_submittable|built_without_approve|edited")]),
    "3.1-auth": ("F1 authentication (sign-in, sessions, lockout)", "unit + integration", [
        ("test_limit_orders|test_sessions|test_dashboard_logic|test_clock_jumps",
         r"sign_in|session|password|lock|identity|scrypt|confirmation|users_file|add_user|guessing")]),
    "3.1-reject": ("F1 rejection recorded, no order", "integration", [
        ("test_trade_rejection|test_dashboard_logic", r"reject|reconsider|audit")]),
    "3.1-audit": ("F1 audit logs (ledger, approval decisions, proposals)", "unit + integration", [
        ("test_alpaca_paper|test_trade_rejection|test_phase3_data_model", r"record|ledger|log|audit|append|proposal")]),
    "3.1-nokeys": ("F1 works without Alpaca keys", "integration (AppTest)", [
        ("test_dashboard_logic|test_alpaca_paper|test_phase3_integration", r"without_an_alpaca|not_configured|no_keys|without_keys")]),
    "3.1-proposals": ("F1 valid / invalid order proposals", "unit", [
        ("test_order_policy|test_limit_orders|test_alpaca_paper|test_trade_rejection",
         r"rating_allows|hold|review|sell|short|quantity|whole|market_orders|crypto|us_stocks|share_classes|limit_prices|one_order_per_run|tied_to_its_run")]),
    "3.2-tools": ("F2 five tools, schemas, read-only annotations", "protocol (in-memory)", [
        ("test_mcp_server|test_mcp_read_tools", r"five_read_only|schemas|over_the_protocol")]),
    "3.2-validation": ("F2 input validation and errors", "protocol (in-memory)", [
        ("test_mcp_server|test_mcp_read_tools|test_mcp_error_labels",
         r"bad_|invalid|refused|malformed|limit|path|missing|validated|include_reports")]),
    "3.2-timeout": ("F2 timeout / cancel", "integration", [
        ("test_mcp_server|test_phase3_timing_and_cancel", r"timeout|cancel")]),
    "3.2-busy": ("F2 one analysis at a time", "protocol", [("test_mcp_server", r"busy")]),
    "3.2-readonly": ("F2 read-only: no execution path", "unit (import pins)", [
        ("test_mcp_server|test_mcp_read_tools", r"cannot_reach_the_broker|only_the_paper_adapter|no_audit_record|propose_trade")]),
    "3.2-http": ("F2 HTTP auth / exposure", "unit + protocol", [
        ("test_mcp_server|test_mcp_read_tools", r"http|token|loopback|tls|reachable|public")]),
    "3.2-budget": ("F2 per-run call / cost limits", "integration", [("test_mcp_budget", r".")]),
    "4.1-shared": ("Shared service (CLI, F1, F2)", "integration", [
        ("test_analysis_service|test_phase3_integration|test_worker", r".")]),
    "4.1-e1": ("E1 routing, fallback, breaker", "unit + integration", [
        ("test_e1_", r".")]),
    "4.1-e2": ("E2 cache and its metrics", "unit + integration", [("test_e2_", r".")]),
    "4.1-e3": ("E3 tracing, tokens, estimated cost", "unit + integration", [
        ("test_e3_|test_cost_accounting|test_e1_e2_e3_audit", r".")]),
    "4.3-ticker": ("Invalid ticker", "unit + protocol", [
        ("test_dashboard_input_errors|test_mcp_server|test_cli_headless|test_cli_symbol", r"ticker")]),
    "4.3-date": ("Future / invalid date", "unit", [
        ("test_common_errors|test_mcp_server|test_cli_headless|test_dashboard", r"date|future")]),
    "4.3-429": ("Gemini 429 / quota", "unit", [("test_e1_quota_and_providers", r".")]),
    "4.3-network": ("Network down / data vendor failure", "unit", [
        ("test_no_data_handling|test_sec_edgar|test_e2_", r"unreachable|no_data|empty|fail|offline|hit")]),
    "4.3-alpacarej": ("Alpaca rejects order", "unit (fake broker)", [
        ("test_alpaca_paper|test_trade_rejection", r"refusal|broker_failure|blocked|failure_is")]),
    "4.3-closed": ("Market closed", "unit (fake clock)", [
        ("test_order_policy|test_limit_orders|test_alpaca_paper", r"closed|after_hours|clock|regular_session")]),
    "4.3-oversize": ("Order above max size", "unit + property", [
        ("test_order_policy|test_limit_orders|test_alpaca_paper", r"cap|exceed|notional")]),
    "4.3-userreject": ("User rejects trade", "integration", [("test_trade_rejection", r"reject")]),
    "4.3-mcpbad": ("MCP bad input", "protocol", [
        ("test_mcp_server|test_mcp_read_tools|test_mcp_error_labels", r"bad|invalid|malformed|refused|include_reports")]),
    "4.3-mcplimit": ("MCP rate / resource limit", "integration", [
        ("test_mcp_server|test_mcp_budget", r"busy|limit|budget")]),
    "4.3-langfuse": ("Langfuse not set up", "unit", [
        ("test_common_errors|test_worker|test_e3_tracing|test_e3_env", r"langfuse|without|disabled|missing|credentials|e3")]),
}

cases = []
for path in sys.argv[1:]:
    for tc in ET.parse(path).iter("testcase"):
        status = "pass"
        if tc.find("failure") is not None or tc.find("error") is not None:
            status = "fail"
        elif tc.find("skipped") is not None:
            status = "skip"
        cases.append((tc.get("classname") or "", tc.get("name") or "", status))

print("| ID | Requirement | Test type | Tests | Passed | Failed | Skipped | Result |")
print("| --- | --- | --- | --- | --- | --- | --- | --- |")
total_fail = 0
for rid, (label, kind, rules) in R.items():
    hits = [c for c in cases for f, n in rules if re.search(f, c[0]) and re.search(n, c[1])]
    hits = list(dict.fromkeys(hits))
    p, f, s = (sum(c[2] == k for c in hits) for k in ("pass", "fail", "skip"))
    total_fail += f
    result = "NOT TESTED" if not hits else "FAIL" if f else "PASS"
    print(f"| {rid} | {label} | {kind} | {len(hits)} | {p} | {f} | {s} | {result} |")
print(f"\nAll collected cases: {len(cases)}; failures among mapped: {total_fail}")
if "--list" in sys.argv:
    pass
