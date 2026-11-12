.PHONY: demo test test-fast clean

# Starts the business systems, an ADK host and a Temporal Worker, plus a local
# Temporal dev server if none is running on localhost:7233, then opens the UI.
demo:
	@(for i in $$(seq 1 60); do curl -sf localhost:8400/api/state >/dev/null && open http://localhost:8400 && exit; sleep 0.5; done) &
	uv run python -m refund_demo.api

# Every act, headless, against real processes and real crashes (about a minute).
test:
	uv run pytest

# Only the tests that need no processes.
test-fast:
	uv run pytest tests/test_units.py

# Forget all demo state: ADK sessions, the ledger, the dev server's history.
clean:
	rm -rf var
