<<<<<<< HEAD
# Parallel Computing Lab - Student ID 230103216

Pure Python 3 (standard library only). Windows + PyCharm.

## Files
- `harness.py` - BenchStats, FNV-1a 64, run_benchmark(), emit_verify_tag(), hardware detection (ctypes/winreg), helpers
- `task0_cal.py` - Phase 0: topology + perf_counter_ns latency (1,000,000 calls)
- `task1_falsesharing.py` - Phase 1: false sharing, scaling, Karp-Flatt
- `task2_scaling.py` - Phase 2: strong/weak scaling, Amdahl/Gustafson
- `task3_roofline_gemm.py` - Phase 3: bandwidth, compute ceiling, GEMM x3, tile sweep
- `task4_scheduling.py` - Phase 4: block / cyclic / dynamic scheduling
- `build_report.py` - builds `lab_report_230103216.md` from the REAL results (no numbers are invented)
- `results/` - phaseN_results.txt written by the scripts

## PyCharm setup
1. Unzip, then File > Open > the folder `parallel_computing_230103216`.
2. File > Settings > Project > Python Interpreter: pick any Python 3.10+ (no packages needed).
3. Open the Terminal tab in PyCharm (PowerShell).

## Before the final run (important for stable numbers)
Plug in the charger, set the Windows power mode to Best performance, close browsers/other apps,
do not touch the laptop while a phase runs.

## Quick smoke test (reduced parameters, a few minutes in total) - NOT for submission
```
python task0_cal.py --quick
python task1_falsesharing.py --quick
python task2_scaling.py --quick
python task3_roofline_gemm.py --quick
python task4_scheduling.py --quick
```

## FINAL run (original assignment parameters) - run in this order
```
python task0_cal.py
python task1_falsesharing.py
python task2_scaling.py
python task3_roofline_gemm.py
python task4_scheduling.py
python build_report.py
```
Expected total time: roughly 15-40 minutes depending on the CPU (Phase 4 and Phase 2 are the longest).
Each script prints its `VERIFY_HASH[PHASEn]=...` line and saves results/phaseN_results.txt.
`build_report.py` refuses to run if any result file comes from a `--quick` run.
Do NOT re-run a single phase after building the report without running build_report.py again.

## If physical-core detection fails
Phase 0 prints a warning and uses Pphys = logical count. Find the real value
(Task Manager > Performance > CPU > "Cores", or PowerShell
`Get-CimInstance Win32_Processor | Select NumberOfCores`) and run, e.g. for 6 cores:
```
$env:PPHYS=6
python task1_falsesharing.py
```
(set the same variable before each script, and in the same PowerShell window before build_report.py).

## Common errors
- `BrokenBarrierError` / hang: a worker crashed; look at the first traceback above it. Close other heavy apps and re-run.
- `FileNotFoundError` on shared memory: a previous run was killed; just re-run.
- Windows Firewall/antivirus prompts: allow Python (multiprocessing uses local pipes only).
- Weird/noisy timings: laptop on battery or throttling; see "Before the final run".

## Documented assumptions
The assignment's provided snippets (BenchStats fields, FNV-1a input serialisation, tag format, LCG constants,
exact FMA/trig operation mix) were not available when this project was generated: they are implemented in
the standard way (see the docstrings) and MUST be compared against the handout before submission.
=======
# Parallel Computing Lab — Performance Evaluation & Optimization (Python)

Student ID по умолчанию: `230103216` (задан в `harness.py` → `DEFAULT_STUDENT_ID`; можно переопределить `--student-id`).
Только стандартная библиотека Python 3.10+. Windows / Linux / macOS (корректно работает со `spawn`).

> Это каркас для измерений. **Никаких результатов в архиве нет** — все числа, хеши и таблицы появятся
> только после запуска на вашем ноутбуке. Перед сдачей проверьте у преподавателя, разрешено ли использовать
> ИИ-помощь (в задании написано «No AI Tools»).

## 1. Создание проекта в PyCharm
1. Распакуйте архив. File → Open → выберите папку `parallel_lab`.
2. Интерпретатор: Python 3.10+ (venv не нужен, пакеты не ставятся).
3. Для удобства: Run → Edit Configurations → Python → Script `task0_cal.py`, Working directory = `parallel_lab`.
   Для отладки добавьте параметр `--quick`.

## 2. Порядок запуска (PowerShell, из папки parallel_lab)
```powershell
python harness.py --selftest          # FNV-1a тест: все строки [OK]
python task0_cal.py                   # Phase 0  (~10 c)
python harness.py --recompute phase0  # должно быть MATCH
python task1_falsesharing.py          # Phase 1  (~3-10 мин)
python task2_scaling.py               # Phase 2  (~4-12 мин)
python task3a_roofline.py             # Phase 3A (~1 мин)
python task3b_gemm.py                 # Phase 3B (~2-8 мин)
python task4_scheduling.py            # Phase 4  (~3-12 мин)
python report_generator.py            # lab_report_230103216.md
```
Каждый скрипт перед длинным замером печатает оценку времени/памяти и спрашивает `[y/N]`
(флаг `--yes` — без вопроса). Закройте браузер и остальные тяжёлые программы, подключите питание, не трогайте
ноутбук во время замеров (иначе шум и «тепловой» троттлинг испортят результаты).

## 3. Быстрый тестовый режим
`--quick` (или `$env:LAB_MODE="quick"`) уменьшает нагрузку; результаты помечаются `"mode": "quick"`,
**хеши из quick-режима недействительны для сдачи**. Перед настоящим прогоном выполните `python task0_cal.py`
без `--quick` заново (скрипты откажутся работать с quick-версией Phase 0 в полном режиме).
Официальные параметры задания в полном режиме не менялись.

## 4. Проверка и возобновление
- JSON: `Get-Content results\phase1.json | Select-Object -First 40`; проверка хеша: `python harness.py --recompute phase1`.
- Возобновление: каждая фаза сохраняется отдельным файлом. Если прогон прервали, просто запустите нужный скрипт снова;
  уже готовые фазы пропускаются (`[resume] ... already exists`), `--force` пересчитывает фазу заново.
- Ctrl+C безопасен: shared memory закрывается и удаляется, пулы завершаются.

## 5. Если не заработало
| Симптом | Решение |
|---|---|
| `results/phase0.json not found` | сначала `python task0_cal.py` |
| `Physical core count unknown` | `python task0_cal.py --assume-physical N` (число физ. ядер из Диспетчера задач) |
| `RuntimeError ... bootstrapping phase` (Windows) | скрипт запущен не как файл; запускайте `python файл.py`, не из консоли `-c` |
| `FileNotFoundError` / `BufferError` для shared memory | завершите зависшие `python.exe` в Диспетчере задач и повторите |
| Зависание на barrier | через 120–300 c сработает таймаут; закройте процессы Python и запустите снова |
| Phase 1: `stride=32 ... skipped` | при >16 физ. ядрах 512-слотный буфер не вмещает stride 32; это нормально, записывается в JSON |
| Нет сигнала false sharing | в чистом Python интерпретатор может скрыть эффект; опишите это честно в отчёте |

## 6. Осознанные отклонения / уточнения от задания (указывайте в отчёте)
1. **Phase 0 hash**: задание не говорит, какие метрики хешировать → metric1 = средняя задержка `perf_counter_ns()` (нс), metric2 = число физ. ядер.
2. **Phase 1/3A**: время trial = максимум времени цикла по воркерам после барьера старта (исключает 50–300 мс старта процессов на Windows). Wall-время со spawn тоже сохраняется.
3. **Phase 1**: набор p = 1, 2, 4, … ≤ Plog плюс P_phys и Plog (нужны для хеша и потолка).
4. **Phase 3A**: в задании «8.0e7 FMA FLOP на воркер», в мастер-промпте «80 000 000 итераций» — противоречие. Использовано задание (8.0e7 FLOP); для другого значения: `--flops-per-worker 2.56e9`.
5. **Phase 3**: «roofline» на чистом Python — это roofline интерпретатора (потолок вычислений на порядки ниже железного).
6. **Phase 2**: Stage A = 5% от N(p), поэтому в weak scaling серийная часть растёт с p — так задано условием.
>>>>>>> f97e06c228e1d76b428cbcabc9202be9465db2b5
