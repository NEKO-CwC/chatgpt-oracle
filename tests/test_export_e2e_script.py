from __future__ import annotations

from pathlib import Path
import runpy


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export-e2e.py"
_attachment_reference_summary = runpy.run_path(str(_SCRIPT))["_attachment_reference_summary"]


def test_attachment_reference_summary_counts_shapes_without_values() -> None:
    summary = _attachment_reference_summary(
        [
            {
                "attachments": [
                    {
                        "file_kind": "library_file",
                        "library_file_id": "private-id-marker",
                        "filename": "private-name-marker",
                    }
                ],
                "rich_parts": [
                    {
                        "content_type": "image_asset_pointer",
                        "asset_pointer": "private-pointer-marker",
                    },
                    {
                        "content_type": "execution_output",
                        "sandbox_path": "/private/path-marker",
                    },
                ],
            }
        ]
    )

    assert summary == {
        "attachment_count": 1,
        "attachment_message_count": 1,
        "attachment_kind_counts": {"library_file": 1},
        "rich_part_count": 2,
        "rich_part_type_counts": {
            "execution_output": 1,
            "image_asset_pointer": 1,
        },
        "reference_field_counts": {
            "asset_pointer": 1,
            "filename": 1,
            "library_file_id": 1,
            "sandbox_path": 1,
        },
    }
    assert "private-id-marker" not in repr(summary)
    assert "private-name-marker" not in repr(summary)
    assert "private-pointer-marker" not in repr(summary)
    assert "private/path-marker" not in repr(summary)
