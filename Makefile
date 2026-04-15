.PHONY: install dev test docker-build docker-up docker-down clean

install:
	pip install -e .

dev:
	pip install -e ".[dev]"

test:
	python -m pytest -q

docker-build:
	docker compose build --pull

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f bot

clean:
	rm -rf build dist *.egg-info .pytest_cache
