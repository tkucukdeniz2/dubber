"""Record which caption tracks each bake-off test video has."""

import argparse
import datetime
import json
import pathlib

DEFAULT_INVENTORY = pathlib.Path("bakeoff/captions_inventory.json")

# A track as the snippet reports it, (language, kind), and the inventory flag it sets.
_FLAGS = {
    ("tr", "manual"): "manual_tr",
    ("en", "manual"): "manual_en",
    ("en", "asr"): "asr_en",
    ("tr", "translated"): "translated_tr",
}


def record(
    inventory_path: pathlib.Path,
    video_id: str,
    tracks: list[dict],
    today: datetime.date | None = None,
) -> dict:
    """Merge one video's caption tracks into the inventory file; return the inventory.

    tracks is the list the snippet prints: dicts with a "lang" and a "kind", which is
    "manual" (human), "asr" (auto-generated) or "translated" (YouTube's auto-translation
    into Turkish). The video's entry holds the flags manual_tr, manual_en, asr_en and
    translated_tr, each true when tracks has that language and kind, and "checked", the
    ISO date of today (or of the date given). A region code counts as its language, so
    "en-GB" is English. A video already in the file gets a fresh entry, the others stay,
    and the file's folder is created when it is missing.
    """
    inventory = {}
    if inventory_path.exists():
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    flags = dict.fromkeys(_FLAGS.values(), False)
    for track in tracks:
        flag = _FLAGS.get((track["lang"].split("-")[0], track["kind"]))
        if flag:
            flags[flag] = True
    checked = (today or datetime.date.today()).isoformat()
    inventory[video_id] = {**flags, "checked": checked}
    inventory_path.parent.mkdir(parents=True, exist_ok=True)
    inventory_path.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    return inventory


def _tracks(text: str) -> list[dict]:
    """The --tracks argument: the JSON list of {"lang", "kind"} the snippet printed."""
    try:
        tracks = json.loads(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not valid JSON ({exc})") from None
    if not isinstance(tracks, list) or not all(
        isinstance(track, dict)
        and isinstance(track.get("lang"), str)
        and isinstance(track.get("kind"), str)
        for track in tracks
    ):
        raise argparse.ArgumentTypeError(
            'expected a JSON list of {"lang": ..., "kind": ...} objects'
        )
    return tracks


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Record which caption tracks a bake-off test video has."
    )
    parser.add_argument("--video", required=True, help="the YouTube video id")
    parser.add_argument(
        "--tracks",
        type=_tracks,
        required=True,
        help="the JSON list that tools/caption_tracks_snippet.js printed",
    )
    parser.add_argument(
        "--inventory",
        type=pathlib.Path,
        default=DEFAULT_INVENTORY,
        help=f"inventory JSON to update (default: {DEFAULT_INVENTORY})",
    )
    args = parser.parse_args(argv)

    try:
        inventory = record(args.inventory, args.video, args.tracks)
    except (ValueError, OSError) as exc:
        parser.error(f"{args.inventory}: {exc}")
    print(f"{args.video} recorded in {args.inventory}: {json.dumps(inventory[args.video])}")


if __name__ == "__main__":
    main()
