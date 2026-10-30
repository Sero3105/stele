PY := .venv/bin/python

.PHONY: install models run test bench

install:            ## venv + dependencies + native Apple Vision helper
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) -r requirements/dev.txt
	$(MAKE) -C native

models:             ## download/verify model weights into ./models (the only network step)
	$(PY) -m app.models download
	$(PY) -m app.models status

run:
	$(PY) -m app.main

test:
	$(PY) benchmarks/make_dataset.py >/dev/null
	$(PY) -m pytest -q tests

bench:
	$(PY) benchmarks/bench_engines.py
	$(PY) benchmarks/bench_vlm_batch.py
	$(PY) benchmarks/bench_scheduler.py
