.PHONY: demo test test-fast e2e e2e-live clean

# Starts the business systems, an ADK host and a Temporal Worker, plus a local
# Temporal dev server if none is running on localhost:7233, then opens the UI.
demo:
	@(for i in $$(seq 1 60); do curl -sf localhost:8400/api/state >/dev/null && open http://localhost:8400 && exit; sleep 0.5; done) &
	uv run python -m refund_demo.api

# Every scenario, headless, against real processes and real crashes (about a minute).
test:
	uv run pytest tests/test_units.py tests/test_acts.py

# Only the tests that need no processes.
test-fast:
	uv run pytest tests/test_units.py

# The page in a browser: every scenario played through the player (about three minutes).
# Once: uv run --group e2e playwright install chromium. E2E_BROWSER_CHANNEL=chrome uses Chrome.
e2e:
	uv run --group e2e pytest tests/e2e/test_ui.py -v

# The deployed demo: a visitor's own stage on Daytona. With DAYTONA_API_KEY set, the test
# deletes the sandboxes it started. E2E_BASE_URL picks another deployment.
E2E_BASE_URL ?= https://temporal-adk-compare.fly.dev
e2e-live:
	E2E_BASE_URL=$(E2E_BASE_URL) uv run --group e2e pytest tests/e2e/test_live.py -v -s

# Forget all demo state: ADK sessions, the ledger, the dev server's history.
clean:
	rm -rf var
