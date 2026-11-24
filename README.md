# PHALANX — Phase 1 radar software

Phase 1 builds the PHALANX radar's software on hardware we already have, so the
signal-processing chain and operator display are proven before the Phase 2 CN0566 array arrives.
It is split into two parts that **share one Python package** (`phalanx/`):

| | Part 1 — Pasternack kit | Part 2 — USRP B210 + the kit's cans |
|---|---|---|
| Folder | [`Part1_Pasternack_Kit/`](Part1_Pasternack_Kit/README.md) | [`Part2_B210_Cantenna/`](Part2_B210_Cantenna/README.md) |
| Radio | Pasternack PEM11002 radar board (built-in FMCW, analog dechirp) | Ettus USRP B210 software-defined radio |
| Link to laptop | USB 2.0, SCPI over NI-VISA | USB 3.0, UHD |
| Samples | real beat signal, 20 kS/s | complex I/Q, up to 30.72 MS/s |
| Range from | FFT of the beat (`range_fft`) | matched filter on a pulsed chirp (`pulse_compress`) |
| Status (27 Oct 2026) | **Complete** — verified on kit s/n 5035 | **Receive works on hardware** (22 Oct); transmit not yet run |

Everything after the range stage (MTI → Doppler FFT → clutter map → CA-CFAR → detections →
display) is the same code for both parts, and for the CN0566 in Phase 2.

## Folder map

```
RDK_Cantenna/
├── README.md                    you are here — start with this, then the Part README you need
├── CHANGELOG.md                 what changed, when, and why (read before changing shared code)
├── phalanx/                     SHARED package — used by both parts; one copy, never duplicated
│   ├── params.py                RadarParams + presets: cantenna_fmcw/cw (Part 1), b210_plfm (Part 2), phaser_plfm (Phase 2)
│   ├── frame.py                 Frame — what crosses hardware → DSP
│   ├── capture.py, session.py   recordings: single .npz files and multi-block sessions (+ verify_session)
│   ├── paths.py                 where recordings go: PHALANX_data/ in your home folder (NOT OneDrive)
│   ├── hardware/
│   │   ├── cantenna.py, codec.py      Part 1 — Pasternack kit driver
│   │   ├── b210.py                    Part 2 — B210 receive-only monitor (run on hardware)
│   │   ├── usrp.py                    Part 2 — B210 pulsed-LFM transmit+receive radar (not yet run)
│   │   ├── simulator.py               both — kit-like (real IF) and SDR-like (complex PLFM) simulators
│   │   └── recorded.py, base.py       both — replay a recording; the common RadarSource interface
│   ├── dsp/                     both — conditioning, range, Doppler, CFAR detection, pipeline
│   └── gui/                     both — operator display (PyQt6 + pyqtgraph)
├── tests/                       tests of the shared package (recording, GUI, common DSP)
├── Part1_Pasternack_Kit/        scripts, docs, tests and dataset that only concern the kit
├── Part2_B210_Cantenna/         scripts, docs, tests that only concern the B210
├── RDK_Cantenna_GUI.py          launch the operator display (either part)
├── RDK_Cantenna_DSP.py          launch the matplotlib diagnostic display (Part 1)
└── pyproject.toml, requirements.txt
```

**Rule of thumb:** a file goes in a Part folder if only that hardware needs it. If both parts
(or Phase 2) use it, it belongs in `phalanx/`, and a change there must keep *both* parts'
tests passing.

## Setup (Windows / PyCharm, Python 3.13)

```
cd RDK_Cantenna
python -m venv .venv            (or let PyCharm create it — keep it inside RDK_Cantenna)
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q             # all three test folders, no hardware needed — 57 passed on 27 Oct 2026
python RDK_Cantenna_GUI.py      # pick "Simulator" and press Start
```

Part 1 also needs NI-VISA (installed on the team laptop). Part 2 needs the Ettus UHD installer
with Python bindings — see the Part 2 README.

## Where recordings go

Recordings are data, not code, and they are big (a B210 session at 1 MS/s is ≈480 MB per minute).
They are written to **`PHALANX_data\captures` in your home folder**
(e.g. `C:\Users\<you>\PHALANX_data\captures`), outside the git clone and outside OneDrive. To
use another folder, set the environment variable `PHALANX_DATA` (PyCharm: Run → Edit
Configurations → Environment variables). Recordings made before 27 Oct 2026 are still in
`RDK_Cantenna\captures\` on the laptop that made them; they replay from there unchanged.

## Version history you can check out

| git tag | what it is |
|---|---|
| `phase1-part1-complete` | Part 1 finished and verified on the kit, plus the first Part 2 receive-monitor work (commit d049673, 22 Oct 2026). The last commit before the folders were split. |

`git checkout phase1-part1-complete` gives the exact code that was demonstrated; `git checkout main`
returns to the current version.

## Working as a team

* Pull before you start, commit small, push when tests pass. Run `python -m pytest -q` before every push.
* Line endings are normalized by `.gitattributes` (repo root) — don't commit files whose only
  change is line endings.
* The clone currently lives in OneDrive. That works for code, but if git ever reports a locked
  or corrupt file, pause OneDrive sync; a clone in a non-synced folder (e.g. `C:\dev\`) is safer.
* Add an entry to `CHANGELOG.md` when you change shared code or move files.

# Project PHALANX RDK Cantenna

This guide sets up the Windows environment for running the RDK Cantenna DSP script and GUI with the USRP B210.

## Reference Setup

| Component | Configuration |
| --- | --- |
| Laptop | Dell Latitude 5431 |
| Operating system | Windows 11 Enterprise, 64-bit |
| Windows version | 25H2 |
| OS build | 26200.9911 |
| SDR | Ettus USRP B210 |
| Native UHD | 4.11.0.0, Windows 11 x64, VS2026 build |
| Python | 3.13, 64-bit |
| Python UHD package | `uhd==4.11.0.0` |

## 1. Install UHD

Download and run the [UHD 4.11.0.0 Windows x64 installer](https://files.ettus.com/binaries/uhd/latest_release/Windows-11-x64/VS2026/uhd_4.11.0.0-release_Win64_VS2026.exe).

If the direct link no longer resolves, open the [Ettus UHD downloads page](https://files.ettus.com/#/binaries/uhd) and look for `uhd_4.11.0.0-release_Win64_VS2026.exe`. Use version 4.11.0.0 for both the native installation and the Python package.

## 2. Open the Repository

1. Clone or select the Project PHALANX repository in GitHub Desktop.
2. Choose **Repository → Open in Visual Studio Code**.
3. Open a PowerShell terminal in VS Code at the repository root.

## 3. Check Python

Run this command to verify that Python 3.13 is available and prints `64`:

```powershell
py -3.13 -c "import struct; print(struct.calcsize('P') * 8)"
```

If Python 3.13 is missing, install it, open a fresh PowerShell terminal, and repeat the check:

```powershell
winget install Python.Python.3.13
```

## 4. Create the Virtual Environment

From the repository root, run each line in PowerShell:

```powershell
Set-Location .\RDK_Cantenna
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install uhd==4.11.0.0
.\.venv\Scripts\python.exe -c "import uhd"
```

The last command should return to the prompt without an error. The commands use the virtual environment’s Python executable directly, so activation is optional.

## 5. Run the DSP and GUI

From the `RDK_Cantenna` folder, start the DSP script:

```powershell
.\.venv\Scripts\python.exe .\RDK_Cantenna_DSP.py
```

Then launch the GUI. If the DSP script is still running, open a second PowerShell terminal in `RDK_Cantenna` for this command:

```powershell
.\.venv\Scripts\python.exe .\RDK_Cantenna_GUI.py
```

## Troubleshooting

If the software cannot find the USRP, connect the B210 over USB and check whether UHD detects it:

```powershell
uhd_find_devices --args "type=b200"
```

Look for `product: B210` in the output. If PowerShell cannot find `uhd_find_devices` and UHD was installed in the default location, add its `bin` folder to the current terminal’s `PATH` and retry:

```powershell
$env:Path = "C:\Program Files\UHD\bin;$env:Path"
uhd_find_devices --args "type=b200"
```

## References

- [Ettus UHD binary installation guide](https://files.ettus.com/manual/page_install_binary.html)
- [UHD 4.11.0.0 Python package](https://pypi.org/project/uhd/4.11.0.0/)
- [Configure VS Code as the GitHub Desktop editor](https://docs.github.com/en/desktop/configuring-and-customizing-github-desktop/configuring-a-default-editor-in-github-desktop)
