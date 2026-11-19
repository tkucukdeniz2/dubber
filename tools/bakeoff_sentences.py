"""Build the sentence set the translation bake-off translates."""

import argparse
import json
import pathlib

from dubber.captions import Sentence, cues_to_sentences, parse_json3


def dedupe_key(text: str) -> str:
    """Normalise whitespace only, so repeats compare equal.

    Case is kept on purpose: str.lower() gets Turkish dotted and dotless I wrong, so
    folding case could merge sentences that are different.
    """
    return " ".join(text.split())


def select_sentences(
    sentences: list[Sentence], n: int, min_words: int = 6
) -> list[Sentence]:
    """Pick n sentences spread evenly by position among the eligible ones.

    A sentence is eligible when it has at least min_words whitespace-separated
    words. With n >= 2 the first and last eligible sentences are always picked; with
    n == 1 the first one is. When fewer than n are eligible, each is returned once.
    """
    eligible = [
        sentence for sentence in sentences if len(sentence.text.split()) >= min_words
    ]
    if not eligible:
        return []
    if n == 1:
        return eligible[:1]
    last = len(eligible) - 1
    picks = sorted({round(i * last / (n - 1)) for i in range(n)})
    return [eligible[i] for i in picks]


def build(inputs: list[pathlib.Path], n: int) -> list[dict]:
    """Pick n sentences spread evenly over the pooled sentences of all the files.

    A sentence whose dedupe_key repeats an earlier one is dropped before the pick.
    Each row's id is "<file stem>:<index>", where the index is the sentence's
    position among all the sentences of its own file.
    """
    # Kept sentences all differ in text, so each one is its own key for its id.
    ids: dict[Sentence, str] = {}
    seen: set[str] = set()
    for path in inputs:
        cues = parse_json3(json.loads(path.read_text(encoding="utf-8")))
        for index, sentence in enumerate(cues_to_sentences(cues)):
            key = dedupe_key(sentence.text)
            if key not in seen:
                seen.add(key)
                ids[sentence] = f"{path.stem}:{index}"
    return [
        {
            "id": ids[sentence],
            "start": sentence.start,
            "end": sentence.end,
            "text": sentence.text,
        }
        for sentence in select_sentences(list(ids), n)
    ]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Pick evenly spread sentences from json3 caption files."
    )
    parser.add_argument("--n", type=int, required=True, help="sentences to pick")
    parser.add_argument("--out", type=pathlib.Path, required=True, help="JSON to write")
    parser.add_argument(
        "files", nargs="+", type=pathlib.Path, metavar="FILE", help="json3 captions"
    )
    args = parser.parse_args(argv)

    rows = build(args.files, args.n)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)
    for row in rows:
        print(f"{row['id']}\t{row['text']}")


if __name__ == "__main__":
    main()
