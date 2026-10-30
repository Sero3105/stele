# Stele — report tecnico

Macchina: **MacBook Pro M4 Pro (12 core), 24 GB memoria unificata, macOS 27.0.1**, Python 3.12.14,
docling 2.145.0, mlx 0.32.4, mlx-vlm 0.7.11. Tutte le misure sono state eseguite su questa macchina;
nessun numero è preso da benchmark esterni. Data: 30/10/2026.

Le tabelle sono generate da `benchmarks/summarize.py` a partire dai JSON in `benchmarks/results/`.

---

## 0. Revisione v3 — output unico, ZIP, ripresa per pagina, interfaccia (30/10/2026)

Questa sezione descrive l'ultima revisione; le sezioni 1–7 restano come storico delle fasi
precedenti (i problemi lì elencati sono ripresi nella tabella 0.2 con il loro stato attuale).
**Tutti i numeri derivano da dataset sintetici**: misurano differenze tra motori, configurazioni
e versioni sulla stessa macchina, non garantiscono la qualità su documenti reali.

### 0.1 Stato per area

| Area | Stato | Verifica |
|---|---|---|
| Output Markdown unico | **Completato.** Un solo `<nome>.md` per file sorgente, qualunque mix di motori; dati per pagina/motore solo nel report interno (`work/`) | `test_pipeline` (PDF misto con 2 motori: ogni testo una volta, ordine pagine, cartella con soli `.md`, `assets/`, `metadata.json`), `test_markdown::test_pages_in_order_without_duplicates`, test ZIP |
| Normalizzazione Markdown | **Completato.** `app/markdown.py`, puro: blocchi tipizzati, paragrafi/tabelle spezzati tra pagine riuniti, righe OCR riunite in paragrafi, numeri di pagina e intestazioni ripetute tolti, titoli senza salti, elenchi/tabelle/formule/immagini uniformi, front matter con i soli metadati presenti nella fonte | 21 test in `test_markdown.py` (incluso "nessuna parola persa" su output tipici dei 4 motori, caratteri speciali, anno in copertina non scambiato per numero di pagina, capitoli diversi non scambiati per intestazioni ripetute) |
| Tabelle | **Completato.** Fallback GLM: struttura di PaddleOCR-VL + testo di GLM; tabella del layout solo se tutti i suoi numeri sono confermati; altrimenti righe di testo. Tabelle su più pagine unite | 6 test in `test_tables.py` su output **reali** della pagina degradata (fixture), benchmark: tabella della scansione a 100 dpi da 0,0 a 0,8 |
| Grafici e immagini | **Completato con limite.** Immagine sempre conservata (`assets/grafico_NNN.png` / `immagine_NNN.png`), didascalia nella posizione del layout, testo letto come testo alternativo; tabella dei dati solo se ogni valore è stampato nell'immagine. Nessun modello aggiunto (Apple Vision + PaddleOCR-VL già installati). Limite: niente dati da grafici senza etichette numeriche | 8 test in `test_charts.py` (output reale di PaddleOCR-VL con intestazione inventata "Categoría" ed etichetta sbagliata: corretti/scartati; valore non stampato → tabella rifiutata; PDF con grafico → tabella corretta) |
| Ripresa dalla pagina interrotta | **Completato.** Checkpoint per pagina con scrittura atomica (tmp + fsync + rename), manifest SHA-256 + modalità + versione, `flock` per file | `test_kill_and_restart_resumes_from_interrupted_page` (kill -9 del server a metà di un PDF scansionato di 48 pagine con marcatori unici: ≥ 8 pagine riprese dal checkpoint, ogni marcatore una volta e in ordine); unit test su scrittura interrotta, JSON troncato, altra versione del file, altra modalità, secondo processo |
| Download ZIP | **Completato.** Streaming nella risposta HTTP, mai su disco; struttura richiesta; README per più documenti; 409 se in corso o se nulla è stato convertito; `Content-Disposition` con nome UTF-8 | 9 test in `test_api.py` (uno/più documenti, nomi duplicati, cartelle, file falliti, nessun file intermedio, intestazioni, 4 download concorrenti identici, archivio da 8 MB prodotto a pezzi) + download reale dal browser |
| Pulizia file temporanei | **Completato.** Dati fuori dal progetto (`~/Library/Application Support/Stele`); `work/` svuotato dei checkpoint a fine file; render di pagina nella temp di sistema; lavori oltre 7 giorni eliminati, mai se attivi o in download; nessuna cache nella radice del progetto | `test_retention_cleanup_spares_active_downloads`, `test_work_files_stay_internal` |
| Interfaccia browser | **Verificata in browser reali (Chrome 156 e WebKit, il motore di Safari).** Safari vero e proprio non è stato automatizzato (richiede `sudo safaridriver --enable`) | 18 test Playwright (9 × 2 motori): file singolo, multipli con errore, cartella, tre modalità, avanzamento, conversione fallita senza ZIP, download ZIP reale, anteprima con tabella e immagine, pannello risorse, layout 1440/1024/390 px senza overflow, zero errori JS e zero risposte HTTP ≥ 400 |
| Metriche memoria | **Completato.** Processo principale, processi figli, MLX (attiva/picco), Docling MPS, picco kernel, budget, GPU % da IOKit senza sudo; "Non disponibile" se una lettura manca | `test_resource_panel_never_invents_values` (browser), verifica manuale dei valori |
| Test automatici | **196 passati, 0 falliti** | `pytest tests` (log in `benchmarks/results/pytest.log`) |

### 0.2 Problemi del report precedente

| Problema | Stato iniziale | Causa | Modifica | Test | Risultato | Limiti residui |
|---|---|---|---|---|---|---|
| Output frammentato | `X.md` + `X.json` + `X.docling.json` + `X_docling/` + `X_assets/` + `X_sheets/`, commenti `<!-- pagina N · motore -->` nel testo | esportazione per motore/pagina pensata per il debug | assemblatore unico + pubblicazione atomica di `<nome>.md`, `assets/`, `metadata.json`; il resto resta interno | test pipeline/API/browser sopra | un solo documento; nessun file intermedio nello ZIP | — |
| Tabelle perse nel fallback GLM | tabella della pagina degradata = 0,0 | GLM a pagina intera restituisce righe semplici | `app/tables.py` | `test_tables.py`, benchmark | 0,0 → 0,8 (4 righe su 5 identiche al riferimento; l'intestazione resta come letta da GLM, `(m³)` invece di `(m²)`) | se le righe GLM non sono separabili (numeri dentro le etichette) restano testo |
| Grafici solo immagine | immagine senza dati | nessuna estrazione | `app/charts.py` + verifica con Apple Vision | `test_charts.py` | dati estratti quando stampati | grafici senza valori stampati: solo immagine |
| Ripresa da inizio file | file interrotto ricominciava da pagina 1 | nessun checkpoint | `app/checkpoint.py` | kill -9 a metà documento | riprende dalla prima pagina non salvata | per i formati non PDF (DOCX, XLSX…) la ripresa è a livello di file (conversione in un solo passaggio) |
| UI non verificata in browser | solo API testata | nessuno strumento browser | Playwright con Chrome di sistema e WebKit | 18 test browser | trovati e corretti 3 bug reali: overflow a 390 px, titolo "Conversione non riuscita" mostrato per un istante prima dell'arrivo dei file, immagini dell'anteprima chieste all'URL sbagliato da Chrome (`<base>` ignorato dal preload scanner in `srcdoc`); più un 404 di favicon | Safari reale non automatizzato |
| Nessuna metrica GPU | "richiede sudo" | `powermetrics` richiede root | utilizzo GPU da `ioreg` (IOAccelerator "Device Utilization %"), leggibile senza privilegi | test browser + verifica | mostrata | valore istantaneo campionato ogni 2 s |
| Didascalie fuori posto | — (emerso nei nuovi test) | Docling mette la didascalia prima dell'immagine | `CaptionPlacement.LAYOUT` | `test_chart_in_pdf_gets_verified_data_table` | didascalia sotto l'immagine come nel PDF | — |
| Paragrafi spezzati nel fallback GLM | — (emerso nell'ispezione dell'output) | GLM restituisce righe visive | riunione dei paragrafi interrotti a metà frase, elenchi compattati | `test_visual_lines_from_ocr_rejoined_into_paragraphs` | paragrafi integri | regola euristica: un paragrafo che termina senza punteggiatura seguito da uno che inizia in minuscolo |
| Dataset sintetico | dichiarato | — | — | — | invariato | resta il limite principale |

### 0.3 Benchmark aggiornati

**Modalità, stesse 31 pagine** (CER/WER sul solo corpo del documento: il nuovo front matter YAML con titolo/data della fonte è escluso dal confronto, perché non fa parte del testo di riferimento; con il front matter incluso il CER risultava artificialmente +1,5–7 punti, verificato su due file).

| Modalità | Pagine | Tempo totale s | s/pagina | CER medio | WER medio | Picco MB | Pagine escalate | Motori usati (pagine) |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| fast | 31 | 20.2 | 0.65 | 0.0212 | 0.0471 | 2300 | 0 | apple-vision 24, docling-ocrmac 7 |
| auto | 31 | 44.7 | 1.44 | 0.0015 | 0.0066 | 5871 | 10 | apple-vision 14, paddleocr-vl-4bit:layout 8, docling-ocrmac 7, glm-ocr-4bit:page 2 |
| advanced | 31 | 69.6 | 2.25 | 0.0018 | 0.0078 | 5869 | 2 | paddleocr-vl-4bit:layout 22, docling-ocrmac 7, glm-ocr-4bit:page 2 |

Prima → dopo la revisione v3 (stessa macchina, stesso dataset):

| Modalità | Tempo s | CER medio | Tabelle (somma punteggi) | Picco MB | Routing per pagina |
|---|---|---|---|---|---|
| fast | 19.6 → 20.2 | 2.12% → 2.12% | 3.8 → 3.8 | 2293 → 2300 | identico |
| auto | 46.2 → 44.7 | 0.19% → 0.15% | 9.0 → 9.8 | 5927 → 5871 | identico |
| advanced | 70.9 → 69.6 | 0.36% → 0.18% | 8.4 → 9.8 | 5885 → 5869 | identico |

Le nuove fasi (assemblaggio, normalizzazione, lettura delle immagini, checkpoint) non hanno cambiato i tempi; la ricostruzione delle tabelle e la pulizia del LaTeX nelle celle hanno migliorato CER e tabelle in Auto e Avanzata. Veloce invariata (non usa GLM né l'estrazione dei grafici).

**Stress, budget 20 GB, 4 worker** (rieseguiti con il codice v3):

| Scenario | Pagine | Tempo s | Picco processo GB | Picco campionato con figli GB | Limite sup. con helper GB | Errori |
|---|---:|---:|---:|---:|---:|---:|
| big100 | 100 | 60.3 | 5.74 | 5.61 | 6.21 | 0 |
| big500 | 500 | 276.5 (prima 277.3) | 5.73 | 5.66 | 6.21 | 0 |
| big_all | 900 | 304.7 (prima 304.7) | 7.18 | 7.45 | 7.66 | 0 |
| mixed50 | 83 | 79.6 | 6.01 | 6.02 | 6.72 | 3 |

Picco massimo osservato in tutta la revisione: **7,66 GB** (limite superiore, tre PDF da 100+300+500 pagine contemporanei) contro un budget di 20 GB. I 3 errori di `mixed50` sono i PDF corrotti inseriti apposta. Il PDF da 300 pagine da solo non è stato rieseguito (100 e 500 coprono l'intervallo; valore precedente 169,3 s).

### 0.4 Spazio su disco

| Elemento | Dimensione |
|---|---:|
| Modelli (`models/`) | 2,6 GB (invariato, nessun modello aggiunto) |
| Ambiente virtuale (`.venv`) | 1,9 GB (di cui Playwright 146 MB, solo per i test browser) |
| Browser WebKit per i test (`~/Library/Caches/ms-playwright`) | 302 MB, fuori dal progetto; rimovibile con `.venv/bin/playwright uninstall` |

---

## 1. Fase 1 — Inventario e pulizia del progetto precedente

### Prima

| Elemento | Percorso | Dimensione | Provenienza | Esclusivo del progetto? |
|---|---|---:|---|---|
| Script web monolitico | `app.py` | 13 KB | progetto | sì |
| Virtualenv Python 3.14 (102 pacchetti: docling 2.124, torch 2.14, transformers 5.8.1, rapidocr 3.9.2, …) | `docling_env/` | 1,4 GB | progetto (`pyvenv.cfg` puntava a questa cartella; interprete rotto dopo l'aggiornamento di Homebrew a 3.14.7) | sì |
| Modelli RapidOCR PP-OCRv6 small det/rec (.onnx + .pth) | dentro `docling_env/…/rapidocr/models` | 59 MB | pacchetto pip | sì |
| Docling layout Heron (`model.safetensors`) | `~/.cache/huggingface/hub/models--docling-project--docling-layout-heron` | 164 MB | scaricato 02/09 19:37 dal primo avvio del progetto (venv creato 02/09 19:11) | sì: nessun'altra installazione di Docling sul sistema (`mdfind`, ricerca nei site-packages, pipx/uv tools) |
| Docling TableFormer fast + accurate | `~/.cache/huggingface/hub/models--docling-project--docling-models` | 342 MB | idem | sì |
| sentence-transformers all-MiniLM-L6-v2 + blob store condiviso | `~/.cache/huggingface/hub/…` | 887 MB | altro strumento (21/09) | **no → non toccato** |
| Lock MinerU / PDF-Extract-Kit | `~/.cache/huggingface/hub/.locks/` | 0 | altro strumento | **no → non toccato** |
| Cartelle di lavoro | `conversioni/caricamenti`, `conversioni/risultati` | vuote | progetto | sì (nessun documento dell'utente presente) |
| Cache Paddle/ONNX/MLX/EasyOCR/torch | — | — | non presenti | — |

### Operazioni

- Eliminati: `docling_env/`, `__pycache__/`, `conversioni/` (vuota), `app.py`.
- I due modelli Docling **non** sono stati cancellati e riscaricati: sono stati **spostati** in
  `models/hf/hub/` (servono anche al nuovo progetto) insieme ai rispettivi lock.
- Nulla di condiviso è stato toccato (`~/.cache/huggingface` è passato da 1,4 GB a 888 MB solo per lo
  spostamento dei modelli Docling; nessuna nuova cache globale creata durante il lavoro).

### Dopo

Spazio libero sul volume: 311 566 776 KB → 313 044 764 KB = **+1,41 GiB recuperati** (misurato con `df`).

---

## 2. Fase 2 — Modelli: ricerca, scelta e verifica

### Candidati valutati

| Candidato | Esito | Motivo (misurato) |
|---|---|---|
| **Apple Vision** (`RecognizeDocumentsRequest` + `RecognizeTextRequest`, helper Swift) | **adottato**: primo passaggio sulle scansioni | 0,4 s/pag a regime, ~128 MB di picco, nessun peso da scaricare, ordine di lettura a colonne corretto, it/en; tabelle deboli (celle perse, virgola decimale letta come punto) |
| Apple Vision `.fast` | scartato | su macOS 27.0.1 restituisce 0 righe su pagine intere (funziona solo su ritagli) |
| **OcrMac** (pyobjc → `VNRecognizeTextRequest`, anche `livetext`) | **adottato solo dentro Docling** | da solo restituisce righe senza layout: CER 64–67% sui documenti a due colonne (righe delle colonne mescolate); dentro Docling (layout Heron) CER 0–2% |
| **Docling** (testo PDF, layout, TableFormer) | **adottato** per pagine digitali e Office | CER 0 sulle pagine digitali, tabelle e titoli 100% |
| RapidOCR (via Docling, backend torch) | scartato | picco 4,9 GB, ordine di lettura errato su due colonne (CER 27%), 2,6 s/pag; onnxruntime non installato e non giustificato |
| **PaddleOCR-VL 1.6** | **adottato, 4-bit**, modalità layout | vedi sotto |
| **GLM-OCR** | **adottato, 4-bit**, pagina intera 1024 px, solo fallback | vedi sotto |
| Surya OCR 2, MinerU, Granite-Docling | non installati | PaddleOCR-VL 1.6 e GLM-OCR sono già supportati nativamente da mlx-vlm su Metal e coprono tabelle/formule; nessun vantaggio atteso che giustificasse altre dipendenze pesanti (MinerU/Surya: PyTorch, nessuna build MLX) |

PaddleOCR-VL 1.6 su Apple Silicon: la pipeline ufficiale richiede PaddlePaddle per il layout; qui
si usa invece il porting MLX di **PP-DocLayoutV3** incluso in mlx-vlm (pesi ufficiali
`PaddlePaddle/PP-DocLayoutV3_safetensors` convertiti in MLX fp32 e verificati con un forward) +
il modello VLM su ritagli di blocco con i prompt ufficiali (`OCR:`, `Table Recognition:`,
`Formula Recognition:`). Così l'intera pipeline gira su Metal senza PaddlePaddle.

### Modelli installati (definitivi)

| Chiave | Repo @ revisione | Formato | Runtime | Disco | Licenza |
|---|---|---|---|---:|---|
| paddleocr-vl-4bit | mlx-community/PaddleOCR-VL-1.6-4bit @ c8987b27 | MLX 4-bit | mlx-vlm / Metal | 682 MB | Apache-2.0 |
| pp-doclayout-v3 | PaddlePaddle/PP-DocLayoutV3_safetensors @ e4f489dc | safetensors → MLX fp32 | mlx-vlm / Metal | 254 MB (127 originale + 127 convertito) | Apache-2.0 |
| glm-ocr-4bit | mlx-community/GLM-OCR-4bit @ 97f58750 | MLX 4-bit | mlx-vlm / Metal | 1196 MB | MIT |
| docling-layout | docling-project/docling-layout-heron | safetensors | torch / MPS | 164 MB | Apache-2.0 |
| docling-tableformer | docling-project/docling-models @ v2.3.0 | safetensors | torch / MPS | 342 MB | Apache-2.0 |
| **Totale** | | | | **2,64 GB** (`du`: `models/` 2,6 GB) | |

Candidati scaricati per i benchmark e poi rimossi con `prune` (≈5,5 GB): PaddleOCR-VL-1.6 bf16
ufficiale, GLM-OCR bf16, GLM-OCR 8-bit. Restano nel registro come `candidate=True`.

**Verifica di `prune`.** Il nuovo huggingface_hub usa uno store di blob condiviso
(`hub/blobs/xx/<hash>` + `.lock` + `.refs`). La prima versione di `prune` avrebbe cancellato anche
i `.lock/.refs` di blob **ancora in uso** (es. il blob di GLM-OCR-4bit): individuato con il dry-run
prima di eseguire, corretto raggruppando i file per hash. Dry-run finale: rimuove solo i blob dei 3
repo candidati; dopo l'esecuzione i 5 modelli tenuti risultano integri e si caricano e convertono
una pagina ciascuno. Un test automatico (`tests/test_units.py::test_prune_*`) verifica che repo e
blob usati non vengano toccati.

---

## 3. Benchmark

### Dataset

Generato da `benchmarks/make_dataset.py` (testo noto ⇒ CER/WER calcolabili): relazione italiana
con tabella, lista e grafico; note inglesi a due colonne con tabella; pagina di formule; ciascuna
scansionata in 3 profili (300 dpi pulita; 150 dpi rumore + sfocatura + rotazione 1,2° + JPEG 45;
100 dpi rumore forte + sfocatura + rotazione −2° + JPEG 30); relazione di sola prosa di 12 pagine;
PDF misto (2 digitali + 1 scansione + 1 digitale); PNG, JPG "fotografato"; DOCX, PPTX con note,
XLSX con celle unite e formule; HTML, CSV, TXT; PDF troncato, file vuoto, formato sconosciuto.
CER/WER: distanza di Levenshtein su testo normalizzato (sintassi Markdown, link alle immagini e
commenti esclusi). "Tabelle" = quota di righe della tabella attesa presenti identiche in una
tabella Markdown; "Titoli" = quota di titoli attesi resi come intestazioni `#`.

### 3.1 Motori singoli (primo giro, stesse 8 scansioni/immagini)

| Motore | Load s | Modello MB | Picco MB | s/pagina (scan) | CER medio scan | CER peggiore | WER medio | Tabelle | Titoli |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| apple-vision-fast | 0.01 | 0 | 99 | 0.08 | 0.9922 | 1.0000 | 0.9952 | 0.00 | 0.00 |
| apple-vision | 0.01 | 0 | 101 | 1.47 | 0.0232 | 0.1238 | 0.0642 | 0.25 | 0.56 |
| docling-ocrmac | 3.37 | 913 | 2828 | 1.77 | 0.0295 | 0.1978 | 0.0614 | 0.57 | 1.00 |
| docling-rapidocr | 1.84 | 282 | 4880 | 2.61 | 0.1313 | 0.3458 | 0.1545 | 0.75 | 1.00 |
| docling | 3.2 | 901 | 2725 | 0.45 | 0.7508 | 1.0000 | 0.7539 | 0.17 | 0.25 |
| glm-ocr-4bit:layout | 2.47 | 1694 | 3460 | 1.91 | 0.0131 | 0.0927 | 0.0402 | 0.90 | 1.00 |
| glm-ocr:layout | 2.61 | 2616 | 4378 | 3.11 | 0.0126 | 0.0878 | 0.0400 | 0.88 | 1.00 |
| glm-ocr:page | 2.63 | 2490 | 4712 | 13.62 | 0.0225 | 0.0382 | 0.0350 | 0.62 | 0.00 |
| ocrmac-livetext | 0.14 | 19 | 331 | 0.22 | 0.4037 | 0.6733 | 0.5114 | 0.00 | 0.00 |
| ocrmac-vision | 0.15 | 20 | 276 | 0.28 | 0.3309 | 0.6606 | 0.4175 | 0.00 | 0.00 |
| paddleocr-vl-4bit:layout | 3.06 | 1379 | 3092 | 2.03 | 0.0128 | 0.0705 | 0.0429 | 0.80 | 1.00 |
| paddleocr-vl:layout | 2.24 | 2525 | 4146 | 2.46 | 0.0131 | 0.0878 | 0.0345 | 0.80 | 1.00 |
| paddleocr-vl:page | 2.61 | 2365 | 3705 | 2.82 | 0.0078 | 0.0477 | 0.0222 | 0.00 | 0.00 |

Note: misure del primo giro, eseguite prima di alcune correzioni successive (pulizia LaTeX delle
celle, euristica titoli di Apple Vision, che ora riconosce il 100% dei titoli del dataset); il
valore 1,47 s/pag di Apple Vision include un primo avvio a freddo di 12 s (a regime 0,36–0,5 s/pag).
`docling` senza OCR non legge le scansioni (CER ~1) ed è riportato come riferimento.

### 3.2 Quantizzazione, risoluzione, batch (stesse 7 pagine)

| Modello | Modalità | Lato max px | s/pagina | Modello MB | Picco MB | CER medio | CER pagine degradate (poor p1/p2) |
|---|---|---:|---:|---:|---:|---:|---|
| glm-ocr-4bit | layout | piena | 1.91 | 1660 | 3513 | 0.0267 | 0.115 / 0.063 |
| glm-ocr-4bit | page | piena | 13.37 | 1532 | 3871 | 0.0292 | 0.016 / 0.019 |
| glm-ocr-4bit | page | 1024 | 1.92 | 1533 | 3237 | 0.0163 | 0.009 / 0.016 |
| glm-ocr-4bit | page | 1280 | 2.77 | 1532 | 3334 | 0.0188 | 0.013 / 0.019 |
| glm-ocr-4bit | page | 768 | 1.26 | 1532 | 3082 | 0.0317 | 0.036 / 0.046 |
| glm-ocr-8bit | layout | piena | 2.36 | 1985 | 3841 | 0.0567 | 0.111 / 0.278 |
| glm-ocr-8bit | page | piena | 13.61 | 1852 | 4192 | 0.0173 | 0.016 / 0.016 |
| glm-ocr-8bit | page | 1024 | 2.24 | 1852 | 3559 | 0.0179 | 0.021 / 0.016 |
| glm-ocr | layout | piena | 3.08 | 2584 | 4439 | 0.0568 | 0.110 / 0.280 |
| glm-ocr | page | 1024 | 2.79 | 2455 | 4099 | 0.0181 | 0.022 / 0.016 |
| paddleocr-vl-4bit | layout | piena | 1.94 | 1344 | 3113 | 0.0228 | 0.080 / 0.056 |
| paddleocr-vl-4bit | layout | piena | 1.93 | 1344 | 3131 | 0.0228 | 0.080 / 0.056 |
| paddleocr-vl | layout | piena | 2.45 | 2458 | 4169 | 0.0260 | 0.111 / 0.060 |

Conclusioni:
- **4-bit ≈ originale** per entrambi i modelli (CER entro ±0,5 punti, a volte meglio), con −1,1 GB
  (Paddle) / −0,9 GB (GLM) e 20–40% più veloce ⇒ scelta 4-bit.
- **GLM-OCR a pagina intera** costa 13,4 s/pag a piena risoluzione indipendentemente dalla
  quantizzazione (il costo è il prefill dei token visivi). Ridimensionando la pagina a **1024 px**:
  **1,92 s/pag** e CER sulle pagine più degradate **0,9% / 1,6%** — il migliore di tutti i motori
  (Paddle layout 8,0% / 5,6%, Apple Vision 12–21%). 768 px degrada (3,6% / 4,6%).
- **Batch inference** (`mlx_vlm.batch_generate`): PaddleOCR-VL non supportato (il processore fallisce
  con immagini di dimensioni diverse, anche con padding a bucket); GLM-OCR batch 4/8 con padding:
  nessun guadagno (1,95–2,01 vs 1,91 s/pag) e testo **peggiore** (CER fino al 14–19% su pagine che a
  batch 1 erano 0,2%). ⇒ batch scartato, codice rimosso.
- Generazione concorrente di più modelli MLX: serializzata da un lock (MLX non garantisce
  valutazioni concorrenti da più thread; la GPU è una sola).

### 3.3 End-to-end: le tre modalità sulle stesse 31 pagine

| Modalità | Pagine | Tempo totale s | s/pagina | CER medio | WER medio | Picco MB | Pagine escalate | Motori usati (pagine) |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| fast | 31 | 19.6 | 0.63 | 0.0212 | 0.0471 | 2293 | 0 | apple-vision 24, docling-ocrmac 7 |
| auto | 31 | 46.2 | 1.49 | 0.0019 | 0.0077 | 5927 | 10 | apple-vision 14, paddleocr-vl-4bit:layout 8, docling-ocrmac 7, glm-ocr-4bit:page 2 |
| advanced | 31 | 70.9 | 2.29 | 0.0036 | 0.0145 | 5885 | 2 | paddleocr-vl-4bit:layout 22, docling-ocrmac 7, glm-ocr-4bit:page 2 |

| File | fast: s / CER / motori | auto: s / CER / motori | advanced: s / CER / motori |
|---|---|---|---|
| scan_clean300_en_twocol.pdf | 0.91 / 0.0073 / apple-vision | 5.93 / 0.0000 / paddleocr-vl | 5.5 / 0.0000 / paddleocr-vl |
| scan_clean300_it.pdf | 1.15 / 0.0014 / apple-vision | 3.35 / 0.0021 / apple-vision+paddleocr-vl | 4.15 / 0.0090 / paddleocr-vl |
| scan_long_it.pdf | 6.01 / 0.0000 / apple-vision | 6.08 / 0.0000 / apple-vision | 32.9 / 0.0026 / paddleocr-vl |
| scan_noisy150_en_twocol.pdf | 0.74 / 0.0073 / apple-vision | 2.78 / 0.0000 / paddleocr-vl | 2.23 / 0.0000 / paddleocr-vl |
| scan_noisy150_it.pdf | 1.18 / 0.0041 / apple-vision | 3.57 / 0.0007 / apple-vision+paddleocr-vl | 4.5 / 0.0076 / paddleocr-vl |
| scan_poor100_en_twocol.pdf | 0.65 / 0.0318 / apple-vision | 2.69 / 0.0073 / paddleocr-vl | 2.23 / 0.0073 / paddleocr-vl |
| scan_poor100_it.pdf | 1.24 / 0.2102 / apple-vision | 9.71 / 0.0118 / glm-ocr | 8.8 / 0.0118 / glm-ocr |
| image_it_table.png | 0.52 / 0.0034 / apple-vision | 2.71 / 0.0023 / paddleocr-vl | 2.19 / 0.0080 / paddleocr-vl |
| image_en_photo.jpg | 0.75 / 0.0073 / apple-vision | 2.81 / 0.0000 / paddleocr-vl | 2.23 / 0.0000 / paddleocr-vl |
| mixed_digital_scan.pdf | 5.53 / 0.0029 / apple-vision+docling-ocrmac | 5.65 / 0.0000 / docling-ocrmac+paddleocr-vl | 5.19 / 0.0000 / docling-ocrmac+paddleocr-vl |
| digital_it.pdf | 0.38 / 0.0000 / docling-ocrmac | 0.4 / 0.0000 / docling-ocrmac | 0.4 / 0.0000 / docling-ocrmac |
| digital_en_twocol.pdf | 0.33 / 0.0000 / docling-ocrmac | 0.34 / 0.0000 / docling-ocrmac | 0.33 / 0.0000 / docling-ocrmac |
| digital_formula_en.pdf | 0.13 / 0.0000 / docling-ocrmac | 0.14 / 0.0000 / docling-ocrmac | 0.14 / 0.0000 / docling-ocrmac |

Nota: la riga *auto* è stata rieseguita dopo le correzioni finali (pulizia unità LaTeX, titoli trasferiti al fallback GLM, router per pagine brevi): routing identico pagina per pagina al run precedente, CER medio 0,28% → 0,19%. *fast* e *advanced* sono del run precedente (per *advanced* la pulizia `m²` avrebbe solo migliorato leggermente il CER).

- **Auto**: qualità migliore (CER medio 0,19%, WER 0,77%) con il 35% del tempo in meno di Avanzata. Sulla
  scansione di sola prosa da 12 pagine Auto impiega **6,0 s contro 32,9 s** (5,5×) con CER 0.
- Le pagine con tabelle passano a PaddleOCR-VL (Apple Vision perde celle); la scansione a 100 dpi
  passa Vision (CER 21%) → Paddle (6% parole errate) → **GLM-OCR (CER 1,2%)**.
- **Veloce**: 2,3× più veloce di Auto, ma tabelle deboli e nessun recupero sulla scansione peggiore.

---

## 4. Memoria, parallelismo, documenti lunghi

### Metodo

- "Picco processo": `ri_lifetime_max_phys_footprint` del kernel (via `proc_pid_rusage`) per il
  processo dell'app — include buffer Metal/MLX e Docling/torch MPS (verificato: 1 GB allocato da MLX
  compare nel phys_footprint ma non nell'RSS).
- "Picco campionato": phys_footprint di processo **+ figli** campionato ogni 0,25 s.
- "Limite sup. con helper": picco processo + 3 × picco dell'helper Apple Vision (ogni helper
  riporta il proprio picco kernel a fine esecuzione; al massimo 3 helper contemporanei).
- PDF lunghi costruiti da pagine reali del dataset in ciclo 4 digitali : 5 scansioni di prosa :
  1 scansione con tabella (100 → 40/50/10 pagine, 500 → 200/250/50).

### Risultati

| Scenario | Budget GB | Worker | File | Pagine | Tempo s | Pagine/min | Picco processo GB | Picco campionato GB | Limite sup. con helper GB | Errori | Motori (file) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|
| light2 | 20 | 4 | 2 | 3 | 4.8 | 38 | 2.33 | 2.33 | 2.33 | 0 | docling-ocrmac 2 |
| light5 | 20 | 4 | 5 | 9 | 4.8 | 112 | 2.34 | 2.35 | 2.34 | 0 | docling-ocrmac 5 |
| light20 | 20 | 4 | 20 | 33 | 6.5 | 305 | 2.5 | 2.51 | 2.5 | 0 | docling-ocrmac 20 |
| heavy2 | 20 | 4 | 2 | 4 | 14.7 | 16 | 3.91 | 3.85 | 3.91 | 0 | glm-ocr-4bit:page 1, paddleocr-vl-4bit:layout 2 |
| heavy2_light5 | 20 | 4 | 7 | 13 | 14.9 | 52 | 5.54 | 5.53 | 6.23 | 0 | apple-vision 2, glm-ocr-4bit:page 1, paddleocr-vl-4bit:layout 2, docling-ocrmac 5 |
| mixed50 | 20 | 4 | 50 | 83 | 77.8 | 64 | 6.11 | 5.88 | 6.82 | 3 | docling-ocrmac 27, apple-vision 23, glm-ocr-4bit:page 4, paddleocr-vl-4bit:layout 23 |
| big100 | 20 | 4 | 1 | 100 | 60.8 | 99 | 5.76 | 5.55 | 6.23 | 0 | apple-vision 1, docling-ocrmac 1, paddleocr-vl-4bit:layout 1 |
| big300 | 20 | 4 | 1 | 300 | 169.3 | 106 | 5.82 | 5.77 | 6.3 | 0 | apple-vision 1, docling-ocrmac 1, paddleocr-vl-4bit:layout 1 |
| big500 | 20 | 4 | 1 | 500 | 277.3 | 108 | 5.72 | 5.58 | 6.21 | 0 | apple-vision 1, docling-ocrmac 1, paddleocr-vl-4bit:layout 1 |
| big_all | 20 | 4 | 3 | 900 | 304.7 | 177 | 7.16 | 7.54 | 7.64 | 0 | apple-vision 3, docling-ocrmac 3, paddleocr-vl-4bit:layout 3 |
| light20 | 20 | 1 | 20 | 33 | 8.8 | 225 | 2.25 | 2.25 | 2.25 | 0 | docling-ocrmac 20 |
| mixed50 | 20 | 1 | 50 | 83 | 105.3 | 47 | 5.8 | 5.7 | 6.51 | 3 | docling-ocrmac 27, apple-vision 23, glm-ocr-4bit:page 4, paddleocr-vl-4bit:layout 23 |
| heavy2_light5 | 6 | 4 | 7 | 13 | 16.6 | 47 | 4.61 | 4.54 | 5.33 | 0 | apple-vision 2, glm-ocr-4bit:page 1, paddleocr-vl-4bit:layout 2, docling-ocrmac 5 |
| mixed50 | 6 | 4 | 50 | 83 | 94.5 | 53 | 4.72 | 4.76 | 5.43 | 3 | docling-ocrmac 27, apple-vision 23, glm-ocr-4bit:page 4, paddleocr-vl-4bit:layout 23 |
| big_all | 6 | 4 | 3 | 900 | 983.0 | 55 | 4.67 | 4.73 | 5.15 | 0 | apple-vision 3, docling-ocrmac 3, paddleocr-vl-4bit:layout 3 |

### Tempi per documenti lunghi (modalità Auto)

| Documento | Pagine (digitali / scansioni prosa / scansioni con tabella) | Tempo | s/pagina | Picco processo | Limite sup. con helper |
|---|---|---:|---:|---:|---:|
| PDF da 100 pagine | 40 / 50 / 10 | **60,8 s** | 0,61 | 5,76 GB | 6,23 GB |
| PDF da 300 pagine | 120 / 150 / 30 | **169,3 s** | 0,56 | 5,82 GB | 6,30 GB |
| PDF da 500 pagine | 200 / 250 / 50 | **277,3 s** | 0,55 | 5,72 GB | 6,21 GB |
| 100 + 300 + 500 insieme | 360 / 450 / 90 | **304,7 s** (in sequenza: 507 s) | 0,34 | 7,16 GB (campionato con figli 7,54) | 7,64 GB |

Routing verificato sul PDF da 500 pagine: 200 pagine Docling, 250 Apple Vision, 50 PaddleOCR-VL
(tutte per "tabelle"), 0 escalation rifiutate. La memoria **non cresce con il numero di pagine**
(5,7–5,8 GB da 100 a 500): il documento Docling di ogni blocco viene scritto su disco e rilasciato
subito (prima della correzione restavano tutti in RAM fino alla fine). Analisi preliminare del PDF da
500 pagine: 0,91 s.

**Budget mai superato** in nessuno scenario: massimo assoluto 7,64 GB (limite superiore) contro 20 GB.
Con budget artificialmente ridotto a **6 GB** lo scheduler resta sotto il limite (picco 4,6–4,8 GB,
limite sup. 5,15–5,43 GB) mettendo in coda i lavori (fino a 95 attese), ma con i tre PDF lunghi
insieme impiega 983 s invece di 304 s: con così poca memoria Docling e PaddleOCR-VL non stanno
entrambi caricati e vengono scaricati/ricaricati a turno.

Parallelismo: 20 file leggeri 6,5 s con 4 worker vs 8,8 s con 1; 50 file misti 77,8 s vs 105,3 s;
100+300+500 pagine 1,66× più veloce che in sequenza. Il guadagno è limitato perché tutti i motori
condividono un'unica GPU/Neural Engine e i pesi non vengono duplicati (scelta voluta).

---

## 5. Verifica finale del codice (revisione precedente)

> Storico. Stato attuale: sezione 0 (tabelle 0.1 e 0.2).

Revisione completa con analisi statica (`ruff`: pyflakes, bugbear, pylint, pyupgrade) e lettura del
codice. Problemi trovati e **corretti**:

| # | Problema | Tipo | Correzione |
|---|---|---|---|
| 1 | Pagina con poche parole (copertina "Capitolo 1") giudicata "qualità bassa" → 3 OCR (Vision → Paddle → GLM) | bug router | sotto 5 parole nessun giudizio di qualità; pagina con inchiostro ma testo vuoto → un solo tentativo con Paddle |
| 2 | Controllo "celle vuote" nel ramo Vision irraggiungibile | codice morto | rimosso |
| 3 | Risultato PaddleOCR-VL per le formule scartato da `better()` (il LaTeX riduce le parole del dizionario) | bug | escalation strutturali (tabelle/formule) accettate se non vuote e non degradate |
| 4 | Comandi LaTeX (`\frac`, `\sqrt`) contati come parole errate → escalation inutile a GLM | bug | matematica esclusa dal controllo ortografico |
| 5 | Import lazy PyObjC (`NSMakeRange`) da più thread: `KeyError` intermittente (riprodotto 4 volte su 10) | concorrenza | import a livello di modulo + test di regressione in processo nuovo |
| 6 | Set `done` modificato dai thread mentre il report lo iterava | concorrenza | lock dedicato |
| 7 | Tutti i documenti Docling dei blocchi tenuti in RAM fino alla fine | memoria | scritti e rilasciati per blocco |
| 8 | Immagini: pagine duplicate nell'analisi (`page_kinds: scanned 2` per un PNG) | bug | analisi azzerata prima di rileggere il PDF generato |
| 9 | `prune` avrebbe cancellato `.lock/.refs` di blob in uso | sicurezza dati | file raggruppati per hash; test dedicato |
| 10 | 50 file `X.docling.pN.json` accanto al Markdown su PDF lunghi | usabilità | sottocartella `X_docling/` |
| 11 | Avanzamento pagine aggiornato solo all'inizio dei blocchi | UI | aggiornato anche a fine blocco |
| 12 | `Context.notes` mai usato; 2 import inutilizzati; confronto NaN `v != v`; `raise` senza `from` | qualità | rimossi / sistemati |
| 13 | `numpy` e `pyobjc-framework-Cocoa` usati direttamente ma dichiarati solo come transitivi | dipendenze | aggiunti a `requirements/base.txt`; `uv pip check`: compatibili |
| 14 | Batch inference (padding a bucket) senza beneficio misurato | complessità | codice rimosso, risultato documentato |

Verifiche senza modifiche: nessun lease di memoria annidato (niente deadlock tra motori); le
generazioni MLX sono serializzate da un lock separato dai lease; un `DocumentConverter` condiviso da
6 thread produce output identico al sequenziale; percorsi di upload validati sulla stringa grezza
(`..`, assoluti, `a//b`, `a/./b`, file nascosti rifiutati); file dei risultati serviti solo dentro la
cartella del lavoro; tutti gli import di terze parti coperti dalle dipendenze dichiarate.

---

## 6. Test (revisione precedente)

> Storico. Stato attuale: sezione 0 (tabelle 0.1 e 0.2).

Ultima esecuzione completa: **132 passati, 0 falliti, 0 saltati** (`pytest tests`, 101 s, log in
`benchmarks/results/pytest.log`). Nella penultima esecuzione 3 test fallivano: due erano i bug 3 e 4
della tabella sopra, il terzo un test di riavvio con una finestra di osservazione troppo stretta
(PDF da 12 pagine): ora usa il PDF da 100 pagine.

| File | Test | Cosa verifica |
|---|---:|---|
| `test_router.py` | 76 | matrice completa modalità × tipo pagina × motore × segnale, assenza di cicli, `better()` |
| `test_units.py` | 24 | qualità/ortografia (anche thread e LaTeX), parser VLM, blocchi Vision, analizzatore, lease di memoria (rifiuto oltre budget, attesa, max paralleli, eviction LRU), percorsi, duplicati, ripresa, `prune` su store condiviso |
| `test_pipeline.py` | 16 | motori reali: PDF digitale (immagini esportate), scansione con tabella → Paddle, Veloce senza VLM, PDF misto con ordine pagine, JPG, PPTX + note, XLSX fogli/formule/celle unite, DOCX, TXT/HTML/CSV, PDF corrotto/vuoto/sconosciuto, annullamento, RTF |
| `test_formats.py` | 10 | TIFF multipagina, BMP, DOC e ODT via textutil, MD, formule scansionate → LaTeX, grafico DOCX esportato, PPT/XLS/ODP/ODS → errore chiaro senza LibreOffice |
| `test_api.py` | 1 | caricamento di una cartella con sottocartelle e nomi uguali, path traversal rifiutato, errori per file non mantengono il batch, ZIP con struttura, anteprima |
| `test_reliability.py` | 5 | **kill -9 del server a metà conversione e riavvio** (ripresa, 100 pagine una sola volta e in ordine), errore motore, memoria esaurita, disco pieno, limite dimensione upload, annullamento, eliminazione lavoro |

Non testati automaticamente: formati PPT/XLS/ODP/ODS **con** LibreOffice (non installato), interfaccia
web nel browser (verificata solo l'API che usa), documenti reali di terzi.

---

## 7. Problemi residui e limitazioni (revisione precedente)

> Storico. Stato attuale: sezione 0 (tabelle 0.1 e 0.2).

- **Dataset sintetico**: pagine generate e degradate artificialmente (it/en). I confronti tra motori
  sono affidabili, i valori assoluti di CER su documenti reali (carta ingiallita, timbri, scrittura
  a mano, foto inclinate) possono essere peggiori.
- **Tabelle in scansioni molto degradate**: dove interviene il fallback GLM-OCR a pagina intera il
  testo è il più accurato, ma la tabella resta testo semplice (i titoli invece sono recuperati).
- **Apple Vision**: livelli dei titoli dedotti dall'altezza del testo; su una scansione molto
  degradata un titolo di secondo livello è uscito come primo livello.
- **Grafici**: estratti come immagini, nessuna estrazione dei dati. Formule nei PDF digitali: testo.
- **Formati LibreOffice** (PPT, XLS, ODP, ODS): non supportati finché LibreOffice non è installato.
- **Budget molto basso** (≤ 6 GB): corretto ma lento per l'alternanza carico/scarico dei modelli.
- **Ripresa dopo riavvio**: a livello di file, non di pagina.
- **GPU**: nessuna metrica in dashboard (serve `sudo powermetrics`). CPU e memoria sì.
- **Batch inference** non disponibile/utile con mlx-vlm 0.7.11 per questi modelli.
- Primo utilizzo di Apple Vision dopo l'avvio del Mac: ~12 s una tantum (caricamento modelli di sistema).
- mlx-vlm stampa alcuni `FutureWarning` di transformers al caricamento di GLM-OCR: innocui.
