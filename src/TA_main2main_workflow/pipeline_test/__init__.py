"""Pipeline test — independent full-test-pipeline flow (ta-pipeline-test).

Runs a human-configured test script (TA_PIPELINE_TEST_SCRIPT) that clones
external test repos into ``~/test_workspace`` and runs pytest, collects
the raw logs from ``~/test_workspace/test_log/<repo>_test_log/``, has the
AI organize them into two Excel reports, fixes failing cases one by one
(category-guided: operator / triton-ascend / npu-ir), and — when code
changed — commits per case and opens a PR.

Independent of the main2main sync flow (ta-kickoff).
"""
