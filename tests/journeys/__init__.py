"""GUI journey suite — daily visual regression for the wlwl-ass frontend.

A "journey" is a YAML file describing a short sequence of GUI actions
(navigate, click, type, wait) and the screenshots to capture along the
way. The runner replays each journey via Playwright, then asks a vision
model whether each screenshot still matches its baseline. Diffs land
in ``tests/journeys/runs/<run_id>/`` for review.

See ``tests/journeys/runner.py`` for the entry point and
``tests/journeys/*.yaml`` for the journey specs.
"""
