.PHONY: backfill test sync deploy

backfill:
	@echo "TODO: python pipeline/backfill.py (not implemented yet)"

test:
	.venv/bin/python -m pytest pipeline/tests

sync:
	@echo "TODO: python pipeline/weekly_sync.py (not implemented yet)"

deploy:
	@echo "TODO: wrangler deploy from api/ (not implemented yet)"
