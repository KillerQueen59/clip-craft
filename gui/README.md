# Clip System — Desktop App

A native-window GUI over the `clipcraft` CLI. Nothing about the pipeline changes:
every button shells out to the exact same `py -3 -m clipcraft …` command you'd type
in the terminal, and streams its log into the window live.

## Run it

Double-click **`Clip System.bat`** in the project root. On first launch it installs
two extra packages (`flask`, `pywebview`) and then opens the app window.

For a proper app launch (no console window, real icon, on your Desktop) run once:

```powershell
powershell -ExecutionPolicy Bypass -File gui\install-shortcut.ps1
```

This drops a **Clip System** shortcut on the Desktop that launches via `pythonw`
with the clapperboard icon (`gui/assets/icon.ico`). Regenerate the icon anytime with
`py -3 gui\assets\make_icon.py`.

Or from a terminal:

```powershell
py -3 -m pip install -r gui\requirements-gui.txt   # once
py -3 gui\app.py
```

### Native touches
- **Menu bar** — File (New Project · Open Projects Folder · Reload), View, Help.
- **Open in Explorer** — reveal a run's output folder or a single clip file.
- **Keyboard** — Ctrl+Enter runs the pipeline, Ctrl+N new project.
- **Browse…** uses the OS file picker (returns the real path — no gigabyte upload).

> Prefer a plain browser instead of a window? Run `py -3 gui\server.py` and open
> <http://127.0.0.1:8765/>. Everything works the same; only the "Browse…" native
> file picker is desktop-window-only (paste the path or a URL instead).

## What each tab does

| Tab | CLI it drives |
|-----|---------------|
| **Run** | `clipcraft run` (and `candidates`) — transcribe → select → render |
| **Review** | reads `output/<run>/clips/manifest.json`, plays each rendered clip |
| **Recut** | edit a clip's transcript, then `clipcraft recut --clip N` |
| **Upload** | `clipcraft upload` (YouTube) and `clipcraft tiktok` |
| **Settings** | edit the project's `config.toml` and `brief.txt` |
| **Doctor** | `clipcraft doctor` — ffmpeg / Ollama / whisper check |

Project picker (top-right) creates and switches between folders under `projects/`.

## Notes

- The server binds to `127.0.0.1` only — it is not reachable from the network.
- File serving is restricted to paths under the project root.
- Long runs (whisper on a full podcast) can take minutes; the log keeps streaming
  and the **Stop** button cancels the underlying process.
