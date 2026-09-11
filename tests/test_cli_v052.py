from pathlib import Path

from cueflow.cli import build_parser


def test_cli_preserves_file_reference_order():
    args = build_parser().parse_args(
        [
            "run",
            "workspace",
            "media.wav",
            "--reference",
            "a.pdf",
            "--reference",
            "b.docx",
            "--keyword",
            "Blackwell",
        ]
    )
    assert args.reference == [Path("a.pdf"), Path("b.docx")]
    assert args.keyword == ["Blackwell"]


def test_retry_has_no_input_mutation_arguments():
    args = build_parser().parse_args(["retry-run", "workspace", "run_123"])
    assert args.run_id == "run_123"
    assert not hasattr(args, "media")
    assert not hasattr(args, "reference")
    assert not hasattr(args, "keyword")


def test_review_requires_explicit_run_and_decisions():
    args = build_parser().parse_args(["review", "workspace", "run_123", "decisions.json"])
    assert args.run_id == "run_123"
    assert args.decisions == Path("decisions.json")
