.PHONY: dev test check backend-check frontend-check

dev:
	bash scripts/dev.sh

test:
	cd backend && if [ -x .venv/bin/python ]; then .venv/bin/python -m pytest -q; else python3 -m pytest -q; fi

backend-check:
	cd backend && if [ -x .venv/bin/python ]; then .venv/bin/python -m compileall -q app tests; else python3 -m compileall -q app tests; fi

frontend-check:
	cd frontend && npm run lint && npm run typecheck && npm run build

check: backend-check frontend-check
