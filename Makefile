.PHONY: up down logs pull-model test evaluate simulate

up:
	cp -n .env.example .env || true
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f rca-engine

pull-model:
	docker compose exec ollama ollama pull qwen3:4b

test:
	cd rca-engine && python -m pytest -q

evaluate:
	cd rca-engine && python -m app.cli evaluate --no-llm

simulate:
	curl -s -X POST localhost:8000/test/incident -H 'Content-Type: application/json' \
	  -d '{"scenario":"provider-timeout","notify":true}' | python -m json.tool
