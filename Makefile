.PHONY: install run dev clean

install:
	pip install -r requirements.txt

run:
	flask run --host=0.0.0.0 --port=5000

dev:
	flask run --host=0.0.0.0 --port=5000 --debug

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	rm timetraveler.db
