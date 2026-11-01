PY := .venv/bin/python

.PHONY: install models run app quick-action test bench

install:            ## venv + dependencies + native Apple Vision helper
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) -r requirements/dev.txt
	$(MAKE) -C native

models:             ## download/verify model weights into ./models (the only network step)
	$(PY) -m app.models download
	$(PY) -m app.models status

run:
	$(PY) -m app.main

app:                ## Stele.app: menu bar icon that runs the server (needs `make install` first)
	rm -rf Stele.app
	mkdir -p Stele.app/Contents/MacOS Stele.app/Contents/Resources
	swiftc -O native/stele_app.swift -o Stele.app/Contents/MacOS/Stele
	sed 's|@ROOT@|$(CURDIR)|' native/Info.plist > Stele.app/Contents/Info.plist
	cp native/Stele.icns Stele.app/Contents/Resources/
	codesign --force --sign - Stele.app
	@echo "Pronto: open Stele.app (o trascinala in /Applicazioni)"

QA := $(HOME)/Library/Services/Converti con Stele.workflow

quick-action:       ## Finder → Quick Actions → "Converti con Stele": runs the CLI, nothing stays running
	rm -rf "$(QA)"
	mkdir -p "$(QA)/Contents"
	cp native/quick_action/Info.plist "$(QA)/Contents/"
	sed 's|@ROOT@|$(CURDIR)|' native/quick_action/document.wflow > "$(QA)/Contents/document.wflow"
	/System/Library/CoreServices/pbs -flush
	@echo "Pronto: clic destro su file o cartelle nel Finder → Azioni rapide → Converti con Stele"

test:
	$(PY) benchmarks/make_dataset.py >/dev/null
	$(PY) -m pytest -q tests

bench:
	$(PY) benchmarks/bench_engines.py
	$(PY) benchmarks/bench_vlm_batch.py
	$(PY) benchmarks/bench_scheduler.py
