#!/usr/bin/env python3
import argparse
import hashlib
import struct
from pathlib import Path


ENTRY_OFFSET = 0x400
ENTRY_SIZE = 0x400
ITEM_COUNT_OFFSET = 0x3C
DATA_MAINTYPE = b"RFSFAT16"
DATA_SUBTYPE = b"DATA_FEX00000000"
VERIFY_SUBTYPE = b"VDATA_FEX0000000"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def word_sum(data: bytes) -> int:
    padded = data + b"\0" * ((-len(data)) % 4)
    words = struct.unpack(f"<{len(padded) // 4}I", padded)
    return sum(words) & 0xFFFFFFFF


def patch_items(image: bytearray, maintype: bytes, subtype: bytes, payload: bytes) -> int:
    item_count = struct.unpack_from("<I", image, ITEM_COUNT_OFFSET)[0]
    patched = 0
    for index in range(item_count):
        entry = ENTRY_OFFSET + index * ENTRY_SIZE
        entry_maintype = bytes(image[entry + 8 : entry + 16])
        entry_subtype = bytes(image[entry + 16 : entry + 32])
        if entry_maintype != maintype or entry_subtype != subtype:
            continue

        unpacked_len = struct.unpack_from("<I", image, entry + 0x124)[0]
        packed_len = struct.unpack_from("<I", image, entry + 0x12C)[0]
        data_offset = struct.unpack_from("<I", image, entry + 0x134)[0]
        if len(payload) != packed_len:
            raise SystemExit(
                f"{maintype.decode()}/{subtype.decode()} entry {index} length mismatch: "
                f"payload=0x{len(payload):x} unpacked=0x{unpacked_len:x} packed=0x{packed_len:x}"
            )
        image[data_offset : data_offset + len(payload)] = payload
        patched += 1
    return patched


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replace DATA_FEX and VDATA_FEX items inside a Phoenix IMG payload."
    )
    parser.add_argument("source_phoenix_img", type=Path)
    parser.add_argument("patched_data_fex", type=Path)
    parser.add_argument("output_phoenix_img", type=Path)
    args = parser.parse_args()

    image = bytearray(args.source_phoenix_img.read_bytes())
    data_payload = args.patched_data_fex.read_bytes()
    verify_payload = struct.pack("<I", word_sum(data_payload))

    data_count = patch_items(image, DATA_MAINTYPE, DATA_SUBTYPE, data_payload)
    verify_count = patch_items(image, DATA_MAINTYPE, VERIFY_SUBTYPE, verify_payload)
    if data_count < 1:
        raise SystemExit("No DATA_FEX item was patched")
    if verify_count < 1:
        raise SystemExit("No VDATA_FEX item was patched")

    args.output_phoenix_img.write_bytes(image)
    print(args.output_phoenix_img)
    print(f"DATA_FEX items patched: {data_count}")
    print(f"VDATA_FEX items patched: {verify_count}")
    print(f"Vdata checksum: 0x{struct.unpack('<I', verify_payload)[0]:08x}")
    print(f"size: {args.output_phoenix_img.stat().st_size}")
    print(f"sha256: {sha256(args.output_phoenix_img)}")


if __name__ == "__main__":
    main()
