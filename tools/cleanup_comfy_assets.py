from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import requests


COMFY_URL = "http://127.0.0.1:8188"
INSTALL_ROOT = Path(r"D:\Comfy-Desktop\ComfyUI-Installs")
COMFY_ROOT = INSTALL_ROOT / "ComfyUI" / "ComfyUI"
SHARED_INPUT = Path(r"D:\Comfy-Desktop\ComfyUI-Shared\input")
SHARED_OUTPUT = Path(r"D:\Comfy-Desktop\ComfyUI-Shared\output")

UNUSED_INPUTS = [
    "ComfyUI_temp_tfxjf_00037_.png",
    "Krea2_turbo_00001_.png",
    "image (3).png",
    "image (4).png",
    "image-fdd10aba-b697-40c6-a989-518f20ea0412.jpg",
    "minimax_一采__00002-audio.mp4",
    "minimax_一采__00003.mp4",
    "vrgdg_placeholder_i2i.png",
]

TEST_OUTPUTS = [
    "Krea2_method_index_test_00001_.png",
    "Krea2_reference_edit_00001_.png",
    "Qwen21_faceonly_test_00001.png",
    "Qwen21_livingroom_test_00001.png",
    "Qwen21_reference_edit_00001.png",
    "Qwen21_reference_edit_00002.png",
]


def _move_to_trash(
    source: Path,
    trash_root: Path,
    moved: list[dict],
    skipped_locked: list[str],
) -> None:
    if not source.is_file():
        return
    destination = trash_root / source.parent.name / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(source), str(destination))
    except PermissionError:
        if destination.is_file():
            destination.unlink(missing_ok=True)
        skipped_locked.append(str(source))
        return
    moved.append(
        {
            "source": str(source),
            "destination": str(destination),
            "bytes": destination.stat().st_size,
        }
    )


def _delete_missing_asset_records() -> int:
    deleted = 0
    while True:
        response = requests.get(
            f"{COMFY_URL}/api/assets",
            params={"tags_all": "missing", "limit": 500},
            timeout=30,
        )
        response.raise_for_status()
        assets = response.json().get("assets", [])
        if not assets:
            return deleted
        for asset in assets:
            delete = requests.delete(
                f"{COMFY_URL}/api/assets/{asset['id']}",
                timeout=30,
            )
            if delete.status_code not in {204, 404}:
                delete.raise_for_status()
                raise RuntimeError(
                    f"Failed to delete asset {asset['id']}: {delete.status_code}"
                )
            deleted += 1


def _delete_asset_ids(asset_ids: set[str]) -> int:
    deleted = 0
    for asset_id in asset_ids:
        response = requests.delete(
            f"{COMFY_URL}/api/assets/{asset_id}",
            timeout=30,
        )
        if response.status_code not in {204, 404}:
            response.raise_for_status()
        if response.status_code == 204:
            deleted += 1
    return deleted


def _list_assets(**params) -> list[dict]:
    assets = []
    offset = 0
    while True:
        response = requests.get(
            f"{COMFY_URL}/api/assets",
            params={"limit": 500, "offset": offset, **params},
            timeout=30,
        )
        response.raise_for_status()
        page = response.json().get("assets", [])
        assets.extend(page)
        if len(page) < 500:
            return assets
        offset += len(page)


def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    trash_root = INSTALL_ROOT / "_asset-trash" / timestamp
    moved: list[dict] = []
    skipped_locked: list[str] = []

    for name in UNUSED_INPUTS:
        _move_to_trash(
            SHARED_INPUT / name,
            trash_root,
            moved,
            skipped_locked,
        )
    for name in TEST_OUTPUTS:
        _move_to_trash(
            SHARED_OUTPUT / name,
            trash_root,
            moved,
            skipped_locked,
        )

    temp_dir = COMFY_ROOT / "temp"
    if temp_dir.is_dir():
        for source in temp_dir.iterdir():
            if source.is_file():
                _move_to_trash(source, trash_root, moved, skipped_locked)

    prune = requests.post(f"{COMFY_URL}/api/assets/prune", timeout=300)
    prune.raise_for_status()
    pruned = prune.json().get("marked")
    target_names = set(UNUSED_INPUTS + TEST_OUTPUTS)
    target_ids = {
        asset["id"]
        for asset in _list_assets()
        if asset.get("name") in target_names
    }
    target_ids.update(
        asset["id"] for asset in _list_assets(tags_all="temp")
    )
    deleted_target_records = _delete_asset_ids(target_ids)
    deleted_records = _delete_missing_asset_records()

    trash_root.mkdir(parents=True, exist_ok=True)
    manifest = trash_root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "moved": moved,
                "skipped_locked": skipped_locked,
                "prune_marked": pruned,
                "deleted_target_records": deleted_target_records,
                "deleted_missing_records": deleted_records,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "trash": str(trash_root),
                "moved_files": len(moved),
                "skipped_locked_files": len(skipped_locked),
                "prune_marked": pruned,
                "deleted_target_records": deleted_target_records,
                "deleted_missing_records": deleted_records,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
