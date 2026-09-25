"""Offline token-classifier comparison. No application hook or automatic downloads.

Author training labels the FIRST subtoken of each word, not the last. This
runner reproduces that alignment. O probability is NOT an ownership confidence.
Threshold is fixed before evaluation; failures do not trigger threshold tuning.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from experiments.owner_context.boundary_gold import (  # noqa: E402
    acceptance_passed,
    expected_positions,
)
from experiments.owner_context.local_compare import artificial, cases  # noqa: E402
from experiments.owner_context.option_catalog import prepare_options  # noqa: E402
from experiments.owner_context.punctuation_projection import (  # noqa: E402
    BoundarySelection,
    project,
    source_map,
)

REVISION = "dd8b9481ed3b3c05110d7e041ecbeb6a1c23b8b0"
WEIGHT_SHA256 = "c44184c34c50a01bd8546c232bcb33152b378a4c0d8355d13ae8a66f53aa0c70"
THRESHOLD = 0.95


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    keep = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "COMSPEC", "SYSTEMDRIVE"}
    for key in list(os.environ):
        if key.upper() not in keep:
            del os.environ[key]
    os.environ.update(
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        TOKENIZERS_PARALLELISM="false",
        PYTHONDONTWRITEBYTECODE="1",
    )

    def audit(event: str, values: tuple[Any, ...]) -> None:
        if event in {"socket.connect", "socket.getaddrinfo", "socket.bind", "socket.sendto"}:
            raise RuntimeError("offline-network-disabled")
        if event in {"subprocess.Popen", "os.system"}:
            raise RuntimeError("offline-subprocess-disabled")

    # Imports can inspect OS details. Inference itself is audited and local-only.
    import torch
    import transformers
    from transformers import BertForTokenClassification, BertTokenizerFast

    sys.addaudithook(audit)
    if tuple(int(p) for p in torch.__version__.split(".")[:2]) < (2, 6):
        raise RuntimeError("safe-weights-loader-version-required")
    if file_hash(args.model_dir / "pytorch_model.bin") != WEIGHT_SHA256:
        raise RuntimeError("model-weight-hash-mismatch")
    torch.set_num_threads(4)
    torch.manual_seed(42)
    model_files = [p for p in args.model_dir.iterdir() if p.is_file()]
    source_files = [
        *ROOT.joinpath("app").rglob("*.py"),
        *Path(__file__).parent.glob("*.py"),
        *Path(__file__).parent.joinpath("fixtures").glob("*.json"),
    ]

    def hashes(paths: list[Path]) -> dict[str, str]:
        return {str(p): file_hash(p) for p in paths}

    model_start, source_start = hashes(model_files), hashes(source_files)
    began = time.monotonic()
    tokenizer = BertTokenizerFast.from_pretrained(
        str(args.model_dir), local_files_only=True, trust_remote_code=False
    )
    model = (
        BertForTokenClassification.from_pretrained(
            str(args.model_dir),
            local_files_only=True,
            trust_remote_code=False,
            weights_only=True,
            attn_implementation="eager",
        )
        .to("cpu")
        .eval()
    )
    report: dict[str, Any] = {
        "scope": "isolated-synthetic-boundary-only-not-owner-or-phone-acceptance",
        "model": "uygarkurt/bert-restore-punctuation-turkish",
        "revision": REVISION,
        "threshold": THRESHOLD,
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "loadSeconds": round(time.monotonic() - began, 3),
        "modelHashes": model_start,
        "sourceHashes": source_start,
        "rows": [],
        "deployed": False,
        "labelAlignment": "first-subtoken-per-word-as-author-training",
    }
    by_id = {c["id"]: c for c in cases()}
    by_id["standalone-answer"] = {
        "snapshot": artificial(
            [
                ("Kim bu görevi yapacak?", "S1"),
                ("Mehmet.", "S1"),
                ("Rapor yarın hazır olacak.", "S1"),
            ]
        )
    }
    for case_id, case in by_id.items():
        began = time.monotonic()
        snapshot = case["snapshot"]
        options = prepare_options(snapshot)
        gold_positions = expected_positions(case_id, snapshot)
        # Mask punctuation only in the MODEL INPUT. Original/display/citations retain it.
        words, spans = [], []
        for match in re.finditer(r"\S+", snapshot.text):
            word = match.group().strip('.,!?;:"“”()[]')
            if word:
                words.append(word)
                spans.append((match.start(), match.end()))
        encoded = tokenizer(words, is_split_into_words=True, return_tensors="pt", truncation=False)
        if encoded["input_ids"].shape[1] > 512:
            raise RuntimeError("context-overflow-no-truncation-allowed")
        first_pieces: dict[int, int] = {}
        for piece, word_id in enumerate(encoded.word_ids()):
            if word_id is not None:
                first_pieces.setdefault(word_id, piece)
        if len(first_pieces) != len(words):
            raise RuntimeError("incomplete-word-alignment")
        with torch.inference_mode():
            probabilities = model(**encoded).logits[0].softmax(dim=-1).tolist()
        selected, scores = [], []
        for option in options:
            position = snapshot.sources[option.owner_source].char_end - 1
            matches = [i for i, (start, end) in enumerate(spans) if start <= position < end]
            if len(matches) != 1:
                raise RuntimeError("ambiguous-boundary-word")
            word_id = matches[0]
            probs = probabilities[first_pieces[word_id]]
            best = max(range(len(probs)), key=probs.__getitem__)
            label = model.config.id2label[best]
            scores.append(
                {
                    "id": option.option_id,
                    "word": words[word_id],
                    "label": label,
                    "probabilities": {model.config.id2label[i]: p for i, p in enumerate(probs)},
                }
            )
            if label == "O" and probs[best] >= THRESHOLD:
                selected.append(option.option_id)
        projection = project(snapshot, BoundarySelection(remove_full_stops=selected))
        row = {
            "case": case_id,
            "original": snapshot.text,
            "modelWords": words,
            "scores": scores,
            "expectedBoundaryPositions": gold_positions,
            "selected": selected,
            "boundaryPassed": projection.changed_positions == gold_positions,
            "falseRemovals": len(set(projection.changed_positions) - set(gold_positions)),
            "missedCorrections": len(set(gold_positions) - set(projection.changed_positions)),
            "projection": asdict(projection),
            "sourceMap": source_map(snapshot, projection),
            "elapsedSeconds": round(time.monotonic() - began, 4),
        }
        report["rows"].append(row)
        sys.stdout.write(
            json.dumps(
                {
                    k: row[k]
                    for k in [
                        "case",
                        "boundaryPassed",
                        "falseRemovals",
                        "missedCorrections",
                        "elapsedSeconds",
                    ]
                }
            )
            + "\n"
        )
        sys.stdout.flush()
    report.update(
        modelHashesStable=hashes(model_files) == model_start,
        sourceHashesStable=hashes(source_files) == source_start,
        boundaryGatePassed=all(r["boundaryPassed"] for r in report["rows"]),
        ownerExtractionTested=False,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report["accepted"] = acceptance_passed(
        report["boundaryGatePassed"], report["sourceHashesStable"], report["modelHashesStable"]
    )
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
