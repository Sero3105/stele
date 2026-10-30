"""Print the Markdown tables used in REPORT.md straight from benchmarks/results/*.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

RES = Path(__file__).resolve().parent / "results"
SCANS = ["scan_clean300_it.pdf", "scan_noisy150_it.pdf", "scan_poor100_it.pdf", "scan_clean300_en_twocol.pdf",
         "scan_noisy150_en_twocol.pdf", "scan_poor100_en_twocol.pdf", "image_it_table.png", "image_en_photo.jpg"]


def load(p: Path) -> dict:
    return json.loads(p.read_text())


def engines() -> None:
    print("| Motore | Load s | Modello MB | Picco MB | s/pagina (scan) | CER medio scan | CER peggiore | WER medio | Tabelle | Titoli |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for p in sorted(RES.glob("engine_*.json")):
        r = load(p)
        fs = [f for f in r["files"] if f["file"] in SCANS and f["cer"] is not None]
        if not fs:
            continue
        pages = sum(f["pages"] for f in fs)
        sp = sum(f["seconds"] for f in fs) / pages
        tabs = [f["table"] for f in fs if f["table"] is not None]
        print(f"| {r['engine']} | {r['load_s']} | {r['model_mb']} | {r['peak_mb']} | {sp:.2f} | "
              f"{sum(f['cer'] for f in fs) / len(fs):.4f} | {max(f['cer'] for f in fs):.4f} | "
              f"{sum(f['wer'] for f in fs) / len(fs):.4f} | {sum(tabs) / len(tabs):.2f} | "
              f"{sum(f['headings'] for f in fs) / len(fs):.2f} |")


def vlmopt() -> None:
    print("| Modello | Modalità | Lato max px | s/pagina | Modello MB | Picco MB | CER medio | CER pagine degradate (poor p1/p2) |")
    print("|---|---|---:|---:|---:|---:|---:|---|")
    for p in sorted(RES.glob("vlmopt_*.json")):
        r = load(p)
        side = p.stem.rsplit("_s", 1)[1] if "_s" in p.stem else "0"
        if "_b4" in p.stem or "_b8" in p.stem:
            continue  # batch runs: reported separately
        cer = r["cer"]
        poor = [v for k, v in cer.items() if "poor100_it" in k]
        print(f"| {r['model']} | {r['mode']} | {side if side != '0' else 'piena'} | {r['s_per_page']} | {r['model_mb']} | "
              f"{r['peak_mb']} | {sum(cer.values()) / len(cer):.4f} | {' / '.join(f'{v:.3f}' for v in poor)} |")


def modes() -> None:
    print("| Modalità | Pagine | Tempo totale s | s/pagina | CER medio | WER medio | Picco MB | Pagine escalate | Motori usati (pagine) |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for m in ("fast", "auto", "advanced"):
        p = RES / f"modes_{m}.json"
        if not p.exists():
            continue
        r = load(p)
        fs = r["files"]
        pages = sum(f["pages"] for f in fs)
        used: dict[str, int] = {}
        for f in fs:
            for e in f["engines"]:
                used[e] = used.get(e, 0) + 1
        esc = sum(len(f["escalations"]) for f in fs)
        print(f"| {m} | {pages} | {r['total_s']} | {r['total_s'] / pages:.2f} | {sum(f['cer'] for f in fs) / len(fs):.4f} | "
              f"{sum(f['wer'] for f in fs) / len(fs):.4f} | {r['peak_mb']} | {esc} | "
              f"{', '.join(f'{k} {v}' for k, v in sorted(used.items(), key=lambda kv: -kv[1]))} |")
    print()
    print("| File | " + " | ".join(f"{m}: s / CER / motori" for m in ("fast", "auto", "advanced")) + " |")
    print("|---|---|---|---|")
    rows: dict[str, list[str]] = {}
    for m in ("fast", "auto", "advanced"):
        p = RES / f"modes_{m}.json"
        if p.exists():
            for f in load(p)["files"]:
                eng = "+".join(sorted({e.split(":")[0].replace("-4bit", "") for e in f["engines"]}))
                rows.setdefault(f["file"], []).append(f"{f['seconds']} / {f['cer']:.4f} / {eng}")
    for k, v in rows.items():
        print(f"| {k} | " + " | ".join(v) + " |")


def scheduler() -> None:
    print("| Scenario | Budget GB | Worker | File | Pagine | Tempo s | Pagine/min | Picco processo GB | Picco campionato GB | Limite sup. con helper GB | Errori | Motori (file) |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|")
    for p in sorted(RES.glob("sched_*.json"), key=lambda p: p.stat().st_mtime):
        r = load(p)
        pages = r.get("pages", 0)
        errs = len(r["errors"])
        print(f"| {r['scenario']} | {r['budget_gb']:.0f} | {r['workers']} | {r['files']} | {pages} | {r['wall_s']} | "
              f"{60 * pages / r['wall_s']:.0f} | {r['peak_gb']} | {r['sampled_max_gb']} | {r.get('bound_gb', '')} | "
              f"{errs} | {', '.join(f'{k} {v}' for k, v in r['engines'].items())} |")


if __name__ == "__main__":
    {"engines": engines, "vlmopt": vlmopt, "modes": modes, "scheduler": scheduler}[sys.argv[1]]()
