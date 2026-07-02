#!/usr/bin/env python3
import argparse
import hashlib
import struct
import zlib
from pathlib import Path


SECTOR_SIZE = 512
BOOT0_SECTOR = 16
BOOTPKG_SECTOR = 32800
CARD_BOOT_SECTOR = 40960

FROM = b"bootcmd=run setargs_nand boot_normal"
TO = b"bootcmd=run sunxi_sprite_test"
RUN_B_BOOTCMD = b"bootcmd=run b"
SPRITE_ENV = b"sunxi_sprite_test=sprite_test read"
BOOT_BASE_ENV = b"boot_base=40960"
CONSOLE_ENV = b"console=ttyS0,115200"
DIRECT_SPRITE_BOOTCMD = b"bootcmd=setenv boot_base 40960;sprite_test read"
HELPER_ENV = b"b=setenv boot_base 40960;sprite_test read"
SETARGS_NAND_PREFIX = b"setargs_nand=setenv bootargs"
SUNXI_CHECKSUM_STAMP = 0x5F0A6C39
SUNXI_PACKAGE_CHECKSUM_OFFSET = 0x14
UBOOT_ITEM_OFFSET = 0xC00
UBOOT_CHECKSUM_OFFSET = 0x0C
UBOOT_LENGTH_OFFSET = 0x14
UBOOT_WORK_MODE_OFFSET = 0xE0
WORK_MODE_CARD_PRODUCT = 0x11
TRY_BURN_KEY_FUNC_OFFSET = 0xEC6C
DO_SPRITE_TEST_FUNC_OFFSET = 0x114B0
SPRITE_CARD_FIRMWARE_START_OFFSET = 0x868DC
GENERIC_FLASH_READ_TARGET = 0x69400
DIRECT_MMC_READ_TARGET = 0x69CBC
SPRITE_GENERIC_READ_CALLS = [
    0x8649C,
    0x8650C,
    0x865B8,
    0x8663C,
    0x866D4,
    0x86DE4,
    0x86E8C,
    0x88F5C,
    0x88FE0,
    0x89464,
    0x8A6DC,
    0x8A738,
    0x8A7B8,
    0x8F1D8,
]
PARTITIONS = [
    ("boot-resource", 36192, 16384),
    ("env", 52576, 32768),
    ("boot", 85344, 65536),
    ("rootfs", 150880, 131072),
    ("recovery", 281952, 65536),
    ("data", 347488, 32768),
    ("data_bak", 380256, 32768),
    ("reserved", 413024, 32768),
    ("UDISK", 445792, 0),
]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def patch_boot_package(data: bytes) -> bytes:
    patched = bytearray(data)
    count = data.count(FROM)
    if count < 1:
        raise SystemExit("normal bootcmd string not found")
    replacement = RUN_B_BOOTCMD + b"\0" * (len(FROM) - len(RUN_B_BOOTCMD))
    patched = bytearray(patched.replace(FROM, replacement))
    add_helper_env(patched)
    patch_uboot_work_mode(patched)
    patch_try_burn_key_to_sprite_test(patched)
    patch_firmware_start_sector(patched)
    patch_sprite_reads_to_mmc(patched)
    update_uboot_checksum(patched)
    update_sunxi_package_checksum(patched)
    return bytes(patched)


def add_helper_env(data: bytearray) -> None:
    pos = 0
    patched = 0
    while True:
        start = data.find(SETARGS_NAND_PREFIX, pos)
        if start < 0:
            break
        end = data.find(b"\0", start)
        if end < 0:
            raise SystemExit("unterminated setargs_nand env")
        slot_len = end - start
        if slot_len < len(HELPER_ENV):
            raise SystemExit("setargs_nand env slot is unexpectedly short")
        data[start:end] = HELPER_ENV + b"\0" * (slot_len - len(HELPER_ENV))
        patched += 1
        pos = end + 1
    if patched < 1:
        raise SystemExit("setargs_nand env slot not found")


def patch_uboot_work_mode(data: bytearray) -> None:
    if data[UBOOT_ITEM_OFFSET + 4:UBOOT_ITEM_OFFSET + 12] != b"uboot\0\0\0":
        raise SystemExit("embedded u-boot item not found at expected offset")
    struct.pack_into(
        "<I",
        data,
        UBOOT_ITEM_OFFSET + UBOOT_WORK_MODE_OFFSET,
        WORK_MODE_CARD_PRODUCT,
    )


def patch_try_burn_key_to_sprite_test(data: bytearray) -> None:
    # In this BSP the "try to burn key" helper runs just before the network/shell
    # fallback. Branching it to do_sprite_test gives product cards a chance to run
    # without requiring an interactive U-Boot prompt.
    src = UBOOT_ITEM_OFFSET + TRY_BURN_KEY_FUNC_OFFSET
    dst = UBOOT_ITEM_OFFSET + DO_SPRITE_TEST_FUNC_OFFSET
    if data[src:src + 4] != bytes.fromhex("08402de9"):
        raise SystemExit("try-burn-key function prologue not found at expected offset")
    imm24 = ((dst - src - 8) >> 2) & 0x00FFFFFF
    branch = 0xEA000000 | imm24
    struct.pack_into("<I", data, src, branch)


def patch_firmware_start_sector(data: bytearray) -> None:
    # Original: mov r0,#1; b sunxi_partition_get_offset
    # Our product-card layout places the Phoenix firmware image at sector 40960
    # (0xa000), matching cardscript.fex [card_boot] start.
    src = UBOOT_ITEM_OFFSET + SPRITE_CARD_FIRMWARE_START_OFFSET
    if data[src:src + 8] != bytes.fromhex("0100a0e328fefdea"):
        raise SystemExit("sprite_card_firmware_start stub not found at expected offset")
    data[src:src + 8] = bytes.fromhex("00000ae31eff2fe1")


def patch_sprite_reads_to_mmc(data: bytearray) -> None:
    for call_site in SPRITE_GENERIC_READ_CALLS:
        src = UBOOT_ITEM_OFFSET + call_site
        expected = arm_bl(call_site, GENERIC_FLASH_READ_TARGET)
        if data[src:src + 4] != expected:
            raise SystemExit(f"generic flash read call not found at 0x{call_site:x}")
        data[src:src + 4] = arm_bl(call_site, DIRECT_MMC_READ_TARGET)


def arm_bl(src_offset: int, dst_offset: int) -> bytes:
    imm24 = ((dst_offset - src_offset - 8) >> 2) & 0x00FFFFFF
    return struct.pack("<I", 0xEB000000 | imm24)


def update_uboot_checksum(data: bytearray) -> None:
    uboot_len = struct.unpack_from("<I", data, UBOOT_ITEM_OFFSET + UBOOT_LENGTH_OFFSET)[0]
    if uboot_len <= 0 or UBOOT_ITEM_OFFSET + uboot_len > len(data):
        raise SystemExit(f"unexpected u-boot length: 0x{uboot_len:x}")

    checksum_pos = UBOOT_ITEM_OFFSET + UBOOT_CHECKSUM_OFFSET
    struct.pack_into("<I", data, checksum_pos, SUNXI_CHECKSUM_STAMP)
    item = data[UBOOT_ITEM_OFFSET:UBOOT_ITEM_OFFSET + uboot_len]
    word_count = len(item) // 4
    checksum = sum(struct.unpack_from(f"<{word_count}I", item, 0)) & 0xFFFFFFFF
    struct.pack_into("<I", data, checksum_pos, checksum)


def update_sunxi_package_checksum(data: bytearray) -> None:
    if data[:13] != b"sunxi-package":
        raise SystemExit("boot package does not start with sunxi-package magic")

    struct.pack_into("<I", data, SUNXI_PACKAGE_CHECKSUM_OFFSET, SUNXI_CHECKSUM_STAMP)
    word_count = len(data) // 4
    checksum = sum(struct.unpack_from(f"<{word_count}I", data, 0)) & 0xFFFFFFFF
    struct.pack_into("<I", data, SUNXI_PACKAGE_CHECKSUM_OFFSET, checksum)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a PhoenixCard-style H6os product card and force U-Boot default env into sprite mode."
    )
    parser.add_argument("phoenix_img", type=Path)
    parser.add_argument("dump_dir", type=Path)
    parser.add_argument("output_image", type=Path)
    args = parser.parse_args()

    boot0 = (args.dump_dir / "boot0_sdcard.fex").read_bytes()
    boot_package = patch_boot_package((args.dump_dir / "boot_package.fex").read_bytes())
    mbr_copies = make_mbr_copies((args.dump_dir / "sunxi_mbr.fex").read_bytes())
    phoenix_img = args.phoenix_img.read_bytes()

    end = CARD_BOOT_SECTOR * SECTOR_SIZE + len(phoenix_img)
    aligned_end = ((end + 1024 * 1024 - 1) // (1024 * 1024)) * (1024 * 1024)

    with args.output_image.open("wb") as out:
        out.truncate(aligned_end)
        out.seek(BOOT0_SECTOR * SECTOR_SIZE)
        out.write(boot0)
        out.seek(BOOTPKG_SECTOR * SECTOR_SIZE)
        out.write(boot_package)
        write_mbr_copies(out, mbr_copies)
        out.seek(CARD_BOOT_SECTOR * SECTOR_SIZE)
        out.write(phoenix_img)

    print(args.output_image)
    print(f"size: {args.output_image.stat().st_size}")
    print(f"sha256: {sha256(args.output_image)}")


def make_mbr_copies(template: bytes) -> list[bytes]:
    if len(template) != 65536 or template[8:16] != b"softw411":
        raise SystemExit("sunxi_mbr.fex does not look like a 64KB softw411 Sunxi MBR")

    copies = []
    for index in range(4):
        chunk = bytearray(template[index * 16384 : (index + 1) * 16384])
        struct.pack_into("<I", chunk, 16, 4)
        struct.pack_into("<I", chunk, 20, index)
        struct.pack_into("<I", chunk, 24, len(PARTITIONS))

        for entry in range(120):
            off = 32 + entry * 128
            chunk[off : off + 128] = b"\0" * 128

        for entry, (name, start, size) in enumerate(PARTITIONS):
            off = 32 + entry * 128
            struct.pack_into("<IIII", chunk, off, 0, start, 0, size)
            chunk[off + 16 : off + 32] = b"DISK" + b"\0" * 12
            encoded_name = name.encode("ascii")[:15]
            chunk[off + 32 : off + 48] = encoded_name + b"\0" * (16 - len(encoded_name))
            struct.pack_into("<I", chunk, off + 48, 0x8000)

        struct.pack_into("<I", chunk, 0, zlib.crc32(chunk[4:]) & 0xFFFFFFFF)
        copies.append(bytes(chunk))

    return copies


def write_mbr_copies(out, copies: list[bytes]) -> None:
    # Avoid sector 16 where boot0 lives, but cover the common BSP probe patterns:
    # consecutive 16KB copies, wider reserved-region copies, and the locations
    # used by earlier Openix experiments.
    placements = [
        (1, 0x10000),
        (2, 0x20000),
        (3, 0x30000),
        (1, 0x40000),
        (2, 0x80000),
        (3, 0x100000),
        (1, 4 * 1024 * 1024),
        (2, 8 * 1024 * 1024),
        (3, 12 * 1024 * 1024),
    ]
    for index, offset in placements:
        out.seek(offset)
        out.write(copies[index])


if __name__ == "__main__":
    main()
