from __future__ import annotations

from cueflow.cli import build_parser


def test_cli_uses_explicit_pdf_and_image_url_options_in_command_order() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "project",
            "media.wav",
            "--image-url",
            "https://example.com/1",
            "--text-file",
            "notes.md",
            "--pdf-url",
            "https://example.com/2",
            "--keyword",
            "Blackwell",
        ]
    )
    assert [item.kind for item in args.references] == [
        "image_url",
        "text_file",
        "pdf_url",
    ]
    assert args.keywords == ["Blackwell"]
    assert "--reference-url" not in parser.format_help()


def test_correct_has_no_media_or_language_argument() -> None:
    args = build_parser().parse_args(["correct", "project", "--keyword", "NVIDIA"])
    assert args.command == "correct"
    assert not hasattr(args, "media")
    assert not hasattr(args, "language")


def test_review_requires_decisions_file() -> None:
    args = build_parser().parse_args(["review", "project", "decisions.json"])
    assert args.command == "review"
