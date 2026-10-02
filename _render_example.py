"""One-off: render a PKA example that pulls out to the wide master ONLY while
2+ people talk at once (diarize-gated), and stays a tracked close-up otherwise.
Captions/title/tts/outro are off so the split mechanic is what you see."""
from pathlib import Path

from clipcraft import config as _cfg
from clipcraft.render import render_clip

proj = Path("projects/Comedy/PKA S2 Ep 33 - Marco & Marlo Membuat PKA Menjadi 21+").resolve()
vid = next((proj / "source").glob("*.mp4")).resolve()
cfg = _cfg.load(proj / "config.toml")

# base = face-reframe close-up; overlay the wide view only during duo spans
cfg.raw["clip"]["fit"] = "triptych"
cfg.raw["stack"] = {
    "enabled": True,
    # one full-frame panel, letterboxed to fit width -> "pull out to wide"
    "panels": [{"rect": {"x": 0, "y": 0, "w": 1920, "h": 1080},
                "height": 1, "mode": "contain", "blur": 18}],
}
cfg.raw.setdefault("diarize", {})
# diarization still gates the wide pull-out: overlay the full master ONLY when 2+
# people talk at once (gate=true -> resolve_spans returns just the duo spans).
cfg.raw["diarize"].update({"enabled": True, "gate": True, "num_speakers": 4})
# base = real audio-visual active-speaker focus cam (Light-ASD): per-frame it crops
# to whoever is actually speaking, then punches in ~1.5x so they fill the frame.
cfg.raw.setdefault("asd", {}).update({"enabled": True, "pretrain": "finetuning_TalkSet.model"})
cfg.raw.setdefault("reframe", {}).update({"mode": "asd", "zoom": 1.5})

# isolate the feature: no captions/title/tts/outro for this smoke example
cfg.raw.setdefault("title", {})["enabled"] = False
cfg.raw.setdefault("tts", {})["enabled"] = False
cfg.raw.setdefault("outro", {})["enabled"] = False
cfg.raw.setdefault("hook", {})["mode"] = "none"

clip = {"start": 140.0, "end": 160.0}
transcript = {"words": []}

out = proj.parent.parent.parent / "experiments" / "pka-duo" / "example_pullout.mp4"
render_clip(vid, clip, transcript, cfg, out, 1080, 1920)
print("WROTE", out)
