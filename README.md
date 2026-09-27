# HP 3458A zajdiagnosztika

Windows-program a HP/Agilent/Keysight 3458A multiméter zajának, driftjének és
bekötési hatásainak mérésére GPIB-n keresztül, rögzített mérési terv szerint.

Verzió: 0.9.7

## Cél

- DCV- és négyvezetékes ellenállásmérési (OHMF) blokkok felvétele a terv szerint
- minden blokk nyers adatainak, beállításainak és időbélyegeinek megőrzése
- statisztika, grafikonok és diagnosztikai jelentés készítése a mért blokkokból

A program nem hoz jó/hibás ítéletet, nem kalibrál, és nem módosít kalibrációs adatot.

## Letöltés

Az önálló Windows-program (`HP3458A-diag.exe`) és a `lab.local.toml` a
[Releases](https://github.com/kvez/hp3458a-dcv10-r10-100-gpib/releases) oldalon található.
A két fájlt ugyanabba a mappába kell tenni.

## Tartalom

| Útvonal | Tartalom |
|---|---|
| `src/hp3458diag/` | Forráskód |
| `scripts/gui.py` | Grafikus felület indítása forrásból |
| `scripts/run.py` | Parancssoros eszköz forrásból |
| `scripts/build_exe.py` | Az exe elkészítése forrásból |
| `packaging/exe_main.py` | Az exe belépési pontja |
| `config/lab.example.toml` | Konfigurációs minta (a szimuláció és az exe-build is használja) |
| `pyproject.toml` | Csomagleírás és függőségek |

## Követelmények

- Windows 10/11
- GPIB-illesztő és VISA-meghajtó (NI-VISA vagy Keysight IO Libraries Suite)
- a 3458A elérhető GPIB-címen (alapértelmezés: `GPIB0::22::INSTR`)
- forrásból futtatáshoz: Python 3.11+, `pip install PySide6 pyqtgraph numpy pyvisa`

Szimulációhoz (`--simulate`) nem kell műszer és VISA.

## Konfiguráció

A `lab.local.toml` az exe-vel azonos mappában legyen (letöltés: Releases; minta:
`config/lab.example.toml`). Fontos mezők:

| Mező | Jelentés |
|---|---|
| `[instrument] resource` | a műszer VISA-címe, pl. `GPIB0::22::INSTR` |
| `[instrument] visa_backend` | üres = alapértelmezett VISA-könyvtár |
| `[instrument] io_timeout_ms` | lekérdezési időtúllépés |
| `[instrument] accepted_identities` | elfogadott `ID?`-válaszok |
| `[validation] reading_profile` | válaszformátum-profil (`hp3458a_rev9_1`) |
| `[validation] stat_crosscheck` | DMM–PC statisztika-összevetés (`dmm_half_quantum`) |

## Mérési terv

40 kötelező + 4 opcionális blokk, 11 bekötési kapu, N = 100 minta blokkonként,
NPLC 1 / 10 / 100. Kb. 10,7 óra.

| Sorozat | Bekötés | Funkció, méréshatár |
|---|---|---|
| A | rövidzár a bemeneten (HI–LO) | DCV 100 mV |
| G-DCV10-SHORT | ugyanaz a rövidzár (az A folytatása) | DCV 10 V |
| B-5V, B-7V05, B-10V | 5 V / 7,05 V / 10 V forrás | DCV 10 V |
| C | 4W Kelvin-rövidzár a bemeneten | OHMF 10 Ω, OCOMP ON, DELAY 1 |
| D | 4W rövidzár a kábel végén | OHMF 10 Ω, OCOMP ON, DELAY 1 |
| E-R001, E-R01, E-R1, E-R10 | 0,01 / 0,1 / 1 / 10 Ω ellenállás | OHMF 10 Ω, OCOMP ON, DELAY 1 |
| E-R100 | 100 Ω ellenállás | OHMF 100 Ω, OCOMP ON, DELAY 1 |
| F-R100-ON-D1, -ON-D0, -OFF-D1, -OFF-D0 | 100 Ω (az E-R100 folytatása) | OHMF 100 Ω, NPLC 100, OCOMP ON/OFF × DELAY 1/0 |
| F-R100-RETURN-ON (opcionális) | 100 Ω | OHMF 100 Ω, NPLC 100, OCOMP ON |
| G-R10-R100 (opcionális) | 10 Ω ellenállás | OHMF 100 Ω |

Stabilizálás a kapu után: DCV 300 s, OHMF 900 s. Azonos bekötésen belül a következő
blokk kapu nélkül indul.

A pontok listája forrásból: `python scripts/run.py --list-plan`.

## Használat (exe)

Indítás:

```console
HP3458A-diag.exe                                  teljes kötelező terv, élő műszer
HP3458A-diag.exe --include-optional               a terv az opcionális pontokkal
HP3458A-diag.exe --tests A-NPLC1,A-NPLC10         csak a megadott pontok
HP3458A-diag.exe --simulate                       szimuláció, műszer nélkül
HP3458A-diag.exe --output D:\meresek              más kimeneti mappa
```

Konfiguráció nélkül az exe hibaüzenettel leáll.

Menet a felületen:

1. Bemelegedés bejelölése, utolsó ACAL ideje, megjegyzés.
2. **Új munkamenet** — azonosítás és alapbeállítás.
3. **ACAL (külön művelet)…** — opcionális; minden bemenet leválasztva. Utána 30 perc stabilizálás.
4. **Indítás** — megjelenik a bekötési kapu a következő eszközzel és a stabilizálási idővel.
5. Bekötés, majd **Bekötve, folytatás**. A stabilizálási idő módosításához ok megadása kell.
6. A sorozat blokkjai egymás után lefutnak; eszközcserénél újra kapu jön.
7. **Export** — CSV, JSON, jelentés és grafikonok.
8. Ablak bezárása — a munkamenet lezárul.

Gombok:

| Gomb | Művelet |
|---|---|
| Szünet kérése | a futó blokk után megáll |
| Megszakítás | a futó blokk leállítása |
| Memória újraolvasása | INVALID blokk memóriájának újraolvasása új mérés nélkül |
| Újramérés | INVALID blokk helyett új mérés |
| Opcionális pont kihagyása | az opcionális sorozat kihagyása a kapuban |
| Helyreállítás | hibaállapot után a busz és a memória helyreállítása |

## Eredmények

Az exe mellett a `data\session-<azonosító>\` mappában:

| Fájl | Tartalom |
|---|---|
| `session.json` | munkamenet adatai és állapota |
| `events.jsonl` | eseménynapló (hash-lánccal) |
| `bus.jsonl` | minden GPIB-parancs és nyers válasz |
| `blocks\*.json` | blokkonként a nyers bájtok, beállítások, statisztika |
| `preflight.json`, `baseline*.json`, `acal-*.json` | azonosítás, alapbeállítás, ACAL |
| `exports\export-<azonosító>\` | `summary.csv`, `raw_readings.csv`, `blocks.json`, `report.md`, `plots\*.svg` |

Blokkállapotok: `VALIDATED` (az adatellenőrzések teljesültek), `INVALID` (a memóriaolvasás
nem adott egyező, érvényes blokkot; az ok a naplóban), `ABORTED`, `FAULT`.

## Használat forrásból

```console
pip install PySide6 pyqtgraph numpy pyvisa
python scripts/gui.py --config lab.local.toml
python scripts/gui.py --simulate
python scripts/run.py --list-plan
python scripts/run.py --export-session data\session-<azonosító>
```

Exe készítése (PyInstaller szükséges):

```console
pip install pyinstaller
python scripts/build_exe.py
```

Eredmény: `dist\HP3458A-diag.exe`.
