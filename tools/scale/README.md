# `tools/scale/` — real-scale acceptance harness

These scripts produce the **baseline of what Prism does today** on a real
project at a real record count. Everything they print was measured on this
machine, against a real `.prism` file.

They are **read-only with respect to `prism/**`**: no product source is
modified, and the reference project is opened through a `mode=ro` SQLite URI
and never written.

| File | What it does |
| --- | --- |
| `acceptance.py` | The scale matrix: builds a tier fixture, opens it in the real main window, measures open-to-ready, the three view distances, pan/zoom/wheel, canvas switching, stop/resume, search and count, resident memory, the SQL row counters, and the five counting points. Also the save→reopen round trip (always on a **copy**). |
| `evidence.py` | Read-only evidence: streaming digests, size/mtime stamps, directory listings, machine/environment capture, the other Python processes running at the same time, and the trace reduction (SQL rows per label). |
| `quality.py` | Pixel fingerprints: non-uniform ratio, mean luminance, standard deviation, unique colour count — the evidence that a screenshot is not a flat colour block and that no far-view thumbnail was passed off as a near-view original. |
| `coldstart.py` | The **cold** catalog path: from-zero build with per-batch timing, phase attribution, main-thread occupancy, cancellation, and verification that the rebuilt cache matches the one it replaced. |

`tools/` is not a package. Every script inserts the repository root and this
directory onto `sys.path` itself, so they can be run directly:

```powershell
.\.venv\Scripts\python.exe tools\scale\acceptance.py --help
```

---

## Prerequisites

* The project virtual environment: `.\.venv\Scripts\python.exe`
  (PyQt6 / SQLite / Pillow are already there; **no new dependency is needed**).
* A real project with real images. The reference used here is
  `E:\Prism-stress-100k\Prism-100k-v1.prism` (51.81 GiB) and the source it was
  grown from is `C:\Users\admin\Desktop\测试.prism` (2.46 GiB, **read only**).
* Free disk on `C:` — the tier fixtures are built there.
* A quiet machine, ideally. The numbers are contaminated by other workers on
  the same cores; every report records the competing Python processes so the
  reader can tell a quiet measurement from a busy one.

---

## 1. Cold start: the four catalog questions

```powershell
.\.venv\Scripts\python.exe tools\scale\coldstart.py `
    --project 'E:\Prism-stress-100k\Prism-100k-v1.prism' `
    --cache "$env:APPDATA\Prism\catalog\Prism-100k-v1.prism.sqlite" `
    --label T100K-coldstart `
    --out artifacts\scale-acceptance\2026-10-22\scale-acceptance `
    --sha256
```

| Flag | Meaning |
| --- | --- |
| `--project` | The `.prism` file. Opened read-only. |
| `--cache` | The catalog sidecar. **This is the file that gets moved aside and rebuilt.** It normally lives under `%APPDATA%\Prism\catalog\`, *not* next to the project. |
| `--label` | Name for the report files. |
| `--out` | Output root. Reports go to `<out>\reports\<label>-coldstart.json`. |
| `--sha256` | Digest the project before **and** after the run (51.8 GiB → several minutes each way). Strongest proof the project was not rewritten. |
| `--no-cancel-test` | Skip the cancellation probe (it builds the catalog twice more). |
| `--cancel-after-batches` | Batches to commit before cancelling. Default 20 (20 × 128 = 2 560 records). |
| `--no-attribution` | Skip the isolated phase attribution pass. |

**What it does, in order**

1. **Preflight** — project size/mtime (and digest), the cache directory
   listing, the `.build-lock` state, and whether the scope is ready.
2. **Backup** — the existing cache (plus `-wal`/`-shm`/`.build-lock`) is
   **renamed** to `<name>.bak-<timestamp>`, never deleted. If the rename
   fails the run stops, because a cold build measured against a warm cache is
   meaningless.
3. **Cold build** — the production `ProjectOpenScope.build()` with the
   production batch size (128). Per-batch wall clock comes from the
   production `on_batch` callback. The build runs in a worker thread while
   the main thread samples its own heartbeat, so the report says how long
   the main thread was actually starved.
4. **Verify** — ready flag and its cost, `assets`/`counts` row counts, the
   index list, and the hot-path queries (`iter_query`, `details`, `counts`).
5. **Compare** — the content digest of the rebuilt cache against the digest
   of the cache it replaced.
6. **Phase attribution** — an isolated single-pass replica of the three
   build steps (source read / JSON projection / cache write), timed
   separately. It is a replica, not a patch of production code.
7. **Cancel probe** — on a scratch cache: start a build, cancel it, check
   that it stopped early, that the scope refuses to report readiness, that
   the checkpoint is resumable, that the process is still usable, and that
   the next build resumes from the checkpoint.

**Expected cost** — 100 k records: ~60–90 s per full build, ~3 min per
sha256 pass, ~20 MB of scratch cache. Total ≈ 8–10 min, < 100 MB of disk
beyond the renamed backup.

> **Measured on this machine (2026-10-22)**: the from-zero build of 100 000
> records took **10.89 / 13.75 / 15.01 s** across three runs
> (median ≈ 13.7 s). The spread is driven by the per-batch **disk commit**
> (75 % of the time), not by CPU contention. The catalog sidecar was
> **54.64 MB** after the rebuild — the older "13.7 MB / 20.84 MB" figures are
> stale: the catalog gained the `counts_by_scope_kind` index.
>
> **If a build is killed mid-flight** the sidecar is left with a hot journal,
> and a read-only reader then fails with
> `sqlite3.OperationalError: attempt to write a readonly database`.
> Recover it by opening the sidecar **once read-write** (that triggers the
> rollback), then check `PRAGMA integrity_check` and `SELECT count(*) FROM assets`
> — a partial catalog correctly reports `scope.ready == False` and is rebuilt.

---

## 2. Build a tier fixture

```powershell
.\.venv\Scripts\python.exe tools\scale\acceptance.py build `
    --source 'C:\Users\admin\Desktop\测试.prism' `
    --tier 10000 `
    --out artifacts\scale-acceptance\2026-10-22\scale-acceptance\_work\T10K
```

| Flag | Meaning |
| --- | --- |
| `--source` | The real project to grow. **Read only**; its digest is compared before and after. |
| `--tier` | Target image-record count. |
| `--out` | Output directory; the fixture is `<out>\project.prism` and `<out>\fixture.json`. |
| `--images-per-canvas` | Records per canvas. Default 100. |
| `--materials` | Material-record count. Default: the source's own. |

**Expected cost** — the fixture copies the source, so it is as large as the
source plus the duplicated records: T1K ≈ 3.6 GB, T10K ≈ 12 GB, T100K is not
built this way (the 51.8 GiB reference project is opened directly).

---

## 3. Measure a project in the real main window

```powershell
.\.venv\Scripts\python.exe tools\scale\acceptance.py measure `
    --project artifacts\scale-acceptance\2026-10-22\scale-acceptance\_work\T10K\project.prism `
    --label T10K-paged-cold `
    --out artifacts\scale-acceptance\2026-10-22\scale-acceptance `
    --tier 10000 --mode paged --cache cold
```

| Flag | Meaning |
| --- | --- |
| `--mode paged` | `PRISM_PAGED_OPEN=1` → the on-demand open path. `--mode full` keeps the legacy full reader, which is the honest counterpart for the SQL row counters. |
| `--cache cold` | The run uses an **isolated settings directory**, so the catalog sidecar and the preview cache are both empty → the cold path. `--cache hot` reuses the same settings directory across runs. |
| `--settings-dir` | Explicit settings directory, if the two runs must share one. |
| `--open-timeout-ms` | Default 900 000 (15 min) — the 100 k cold open builds the catalog first. |
| `--no-screenshots` | Skip the PNG capture (the quality fingerprints still run). |

**Expected cost** — T10K: ~1–3 min; T100K: ~10–20 min (the cold open builds
the catalog for 100 k records first). Screenshots: 3 per run.

---

## 4. Save → reopen, always on a copy

```powershell
.\.venv\Scripts\python.exe tools\scale\acceptance.py roundtrip `
    --source artifacts\scale-acceptance\2026-10-22\scale-acceptance\_work\T10K\project.prism `
    --label T10K-roundtrip `
    --out artifacts\scale-acceptance\2026-10-22\scale-acceptance
```

The original is **never** the target of a save. The copy has to fit the disk
budget; `--allow-large-copy` lifts the 20 GiB guard (51.8 GiB → 10+ min of
copying, which is why it is opt-in).

---

## Output layout

```
artifacts/scale-acceptance/2026-10-22/
  BASELINE.md                       <- the human-readable baseline
  baseline.json                     <- the same numbers, structured
  scale-acceptance/
    reports/<label>-coldstart.json  <- cold start, per label
    reports/<label>.json            <- scale matrix, per label
    reports/<label>-roundtrip.json  <- save/reopen, per label
    screens/<label>-{far,mid,near}.png
    trace/<label>/Prism-operations-*.jsonl
    _work/                          <- fixtures and scratch (not evidence)
```

---

## Reading the numbers

* **Record count, unique source images and actually-decoded bytes are three
  separate columns.** Never merge them into one "asset count": the reference
  project holds 100 000 records built from 2 744 distinct source images, so
  the record scale and the decode load differ by ~36×.
* **Every number carries its scale and its environment** (32 logical cores,
  how many other Python processes were competing).
* **A number without a measurement says "无实测" (no measurement).** It is
  never replaced by an inference.
* `pytest` pass counts are **not** performance results.
