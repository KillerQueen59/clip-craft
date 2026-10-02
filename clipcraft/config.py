"""Configuration loading and ffmpeg discovery."""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"


@dataclass
class Config:
    raw: dict
    root: Path
    work_dir: Path
    ffmpeg: str
    ffprobe: str

    @property
    def transcribe(self) -> dict:
        return self.raw.get("transcribe", {})

    @property
    def select(self) -> dict:
        return self.raw.get("select", {})

    @property
    def clip(self) -> dict:
        return self.raw.get("clip", {})

    @property
    def captions(self) -> dict:
        return self.raw.get("captions", {})

    def formats(self, names: list[str] | None = None) -> list[dict]:
        """Resolve output formats to [{name, width, height}]. `names` overrides
        the config's [output].render list."""
        defs = self.raw.get("formats", {}) or {
            "mobile": {"width": 1080, "height": 1920},
        }
        wanted = names or self.raw.get("output", {}).get("render", list(defs.keys()))
        out = []
        for name in wanted:
            spec = defs.get(name)
            if not spec:
                raise ValueError(
                    f"unknown format '{name}'. Defined: {', '.join(defs) or '(none)'}"
                )
            out.append({"name": name, "width": int(spec["width"]),
                        "height": int(spec["height"])})
        if not out:
            raise ValueError("no output formats resolved.")
        return out


# Common install locations ffmpeg lands in on Windows without touching PATH.
_WINGET_GLOBS = [
    r"C:\Users\*\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg*\**\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
]


def find_binary(name: str, configured: str = "") -> str:
    """Return an absolute path to a binary, or "" if not found.

    Order: explicit config value -> PATH -> known Windows install dirs.
    """
    if configured:
        p = Path(configured)
        if p.exists():
            return str(p)
    on_path = shutil.which(name)
    if on_path:
        return on_path
    exe = name if name.endswith(".exe") or os.name != "nt" else name + ".exe"
    import glob

    for pattern in _WINGET_GLOBS:
        pattern = pattern.replace("ffmpeg.exe", exe)
        for hit in glob.glob(pattern, recursive=True):
            if Path(hit).exists():
                return hit
    return ""


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_toml(path: Path) -> dict:
    if path and path.exists():
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    return {}


def _apply_words_file(raw: dict, project_dir: Path) -> None:
    """Seed transcribe vocab from a project Words.txt. Words.txt is free-form:
    comma- or newline-separated terms, with `#` line comments ignored.

    Words.txt seeds `initial_prompt` (biases the first decode window) only.
    It is deliberately NOT auto-applied as `hotwords`: hotwords bias the WHOLE
    file and, on long-form podcasts, whisper echoes them back as fake caption
    tails during silence/music ("Sun Go Kong Sun Go Kong"). A project that truly
    wants whole-file biasing can still set `[transcribe] hotwords` explicitly
    (config always wins). To fix a clip whose captions were echoed/hallucinated,
    use `recut --clip N --retranscribe` (prompt-free, hotword-free)."""
    tconf = raw.setdefault("transcribe", {})
    have_prompt = bool(str(tconf.get("initial_prompt", "") or "").strip())
    if have_prompt:
        return
    for name in ("Words.txt", "words.txt"):
        wf = project_dir / name
        if wf.exists():
            break
    else:
        return
    terms: list[str] = []
    for line in wf.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        terms.extend(t.strip() for t in line.split(",") if t.strip())
    if not terms:
        return
    tconf["initial_prompt"] = ", ".join(terms) + "."


def _apply_people_file(raw: dict, project_dir: Path) -> None:
    """Seed the participant roster from a project People.txt (comma/newline
    separated, `#` comments ignored). The roster is injected into the clip-
    selection prompt so titles/descriptions only use the REAL people in the video
    and the model can't crown a one-off mis-transcribed token (e.g. a stray
    "Robby") as a protagonist. Config `[select].people` always wins. The same
    names also seed the transcription vocab (so they spell right and get caught),
    the video. Config `[select].people` always wins. Put the same names in
    Words.txt too so whisper spells and catches them."""
    sconf = raw.setdefault("select", {})
    if sconf.get("people"):
        return
    for name in ("People.txt", "people.txt"):
        pf = project_dir / name
        if pf.exists():
            break
    else:
        return
    people: list[str] = []
    for line in pf.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            people.extend(t.strip() for t in line.split(",") if t.strip())
    if people:
        sconf["people"] = people


def _apply_corrections_file(raw: dict, project_dir: Path) -> None:
    """Load a project corrections.txt into transcribe.corrections.

    One rule per line, `wrong => right` (or `wrong = right`); `#` line comments
    ignored. Fixes systematic Whisper mishearings (names, slang) with a whole-word
    case-insensitive replace applied after decode. Config wins if it already set
    transcribe.corrections."""
    tconf = raw.setdefault("transcribe", {})
    if tconf.get("corrections"):
        return
    cf = project_dir / "corrections.txt"
    if not cf.exists():
        return
    rules: list[list[str]] = []
    for line in cf.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        sep = "=>" if "=>" in line else ("=" if "=" in line else "")
        if not sep:
            continue
        wrong, right = line.split(sep, 1)
        wrong = wrong.strip()
        if wrong:
            rules.append([wrong, right.strip()])
    if rules:
        tconf["corrections"] = rules


def load(config_path: Path | None = None) -> Config:
    # A project config is merged on top of the base config.toml, so project
    # files only need to list what differs.
    raw = _read_toml(CONFIG_PATH)
    if config_path and Path(config_path).resolve() != CONFIG_PATH.resolve():
        raw = _deep_merge(raw, _read_toml(Path(config_path)))
        # A project-level Words.txt is the curated domain vocabulary (names,
        # brands, terms). Whisper can't spell names it has never seen, so we feed
        # it as the transcription initial_prompt. It only fills in when the config
        # didn't set an explicit initial_prompt, so config always wins.
        _apply_people_file(raw, Path(config_path).parent)
        _apply_words_file(raw, Path(config_path).parent)
        _apply_corrections_file(raw, Path(config_path).parent)

    paths = raw.get("paths", {})
    work_dir = ROOT / paths.get("work_dir", "work")
    work_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = find_binary("ffmpeg", paths.get("ffmpeg", ""))
    ffprobe = find_binary("ffprobe", paths.get("ffprobe", ""))
    # ffprobe ships next to ffmpeg; derive it if only ffmpeg was found.
    if ffmpeg and not ffprobe:
        cand = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
        if cand.exists():
            ffprobe = str(cand)

    return Config(
        raw=raw,
        root=ROOT,
        work_dir=work_dir,
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
    )
