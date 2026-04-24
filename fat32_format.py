"""
Minimal FAT32 formatter for Windows.

Formats a removable drive as FAT32 with 64 KB clusters, bypassing the
Windows 32 GB limitation.  Uses raw disk access via Win32 API.

This is equivalent to what fat32format.exe / guiformat.exe does.

Usage from Python:
    from fat32_format import format_fat32
    format_fat32("D")  # formats drive D: as FAT32

Only works on Windows.  Requires Administrator privileges.
"""

import ctypes
import ctypes.wintypes
import struct
import os
import sys

# Win32 constants
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_SHARE_READ = 1
FILE_SHARE_WRITE = 2
INVALID_HANDLE_VALUE = ctypes.wintypes.HANDLE(-1).value
FSCTL_LOCK_VOLUME = 0x00090018
FSCTL_UNLOCK_VOLUME = 0x0009001C
FSCTL_DISMOUNT_VOLUME = 0x00090020
IOCTL_DISK_GET_DRIVE_GEOMETRY_EX = 0x000700A0
IOCTL_DISK_GET_LENGTH_INFO = 0x0007405C

kernel32 = ctypes.windll.kernel32


def _open_volume(drive_letter):
    """Open a volume for raw read/write. Returns handle."""
    path = f"\\\\.\\{drive_letter}:"
    h = kernel32.CreateFileW(
        path, GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None, OPEN_EXISTING, 0, None)
    if h == INVALID_HANDLE_VALUE:
        raise OSError(f"Cannot open {path} — run as Administrator")
    return h


def _ioctl(handle, code, in_buf=None, out_size=256):
    """Call DeviceIoControl."""
    out_buf = ctypes.create_string_buffer(out_size)
    bytes_returned = ctypes.wintypes.DWORD(0)
    ok = kernel32.DeviceIoControl(
        handle, code,
        in_buf, len(in_buf) if in_buf else 0,
        out_buf, out_size,
        ctypes.byref(bytes_returned), None)
    if not ok:
        raise OSError(f"DeviceIoControl failed: 0x{code:08X}, "
                      f"error={ctypes.GetLastError()}")
    return out_buf.raw[:bytes_returned.value]


def _write_at(handle, offset, data):
    """Write data at a specific byte offset on the volume."""
    high = ctypes.wintypes.DWORD(offset >> 32)
    low = kernel32.SetFilePointer(handle, offset & 0xFFFFFFFF,
                                  ctypes.byref(high), 0)
    if low == 0xFFFFFFFF and ctypes.GetLastError() != 0:
        raise OSError(f"SetFilePointer failed at offset {offset}")
    written = ctypes.wintypes.DWORD(0)
    buf = ctypes.create_string_buffer(data)
    ok = kernel32.WriteFile(handle, buf, len(data),
                            ctypes.byref(written), None)
    if not ok:
        raise OSError(f"WriteFile failed at offset {offset}, "
                      f"error={ctypes.GetLastError()}")
    return written.value


def _get_volume_size(handle):
    """Get volume size in bytes."""
    data = _ioctl(handle, IOCTL_DISK_GET_LENGTH_INFO, out_size=16)
    return struct.unpack('<Q', data[:8])[0]


def _build_boot_sector(total_sectors, sectors_per_cluster, reserved_sectors,
                        num_fats, fat_size_sectors, volume_label="SWITCH"):
    """Build a FAT32 boot sector (512 bytes)."""
    bs = bytearray(512)

    # Jump instruction
    bs[0:3] = b'\xEB\x58\x90'

    # OEM name
    bs[3:11] = b'SMSHNTFM'

    # BPB (BIOS Parameter Block)
    bytes_per_sector = 512
    struct.pack_into('<H', bs, 11, bytes_per_sector)       # BytsPerSec
    bs[13] = sectors_per_cluster                            # SecPerClus
    struct.pack_into('<H', bs, 14, reserved_sectors)        # RsvdSecCnt
    bs[16] = num_fats                                       # NumFATs
    struct.pack_into('<H', bs, 17, 0)                       # RootEntCnt (0 for FAT32)
    struct.pack_into('<H', bs, 19, 0)                       # TotSec16 (0 for FAT32)
    bs[21] = 0xF8                                           # Media (fixed disk)
    struct.pack_into('<H', bs, 22, 0)                       # FATSz16 (0 for FAT32)
    struct.pack_into('<H', bs, 24, 63)                      # SecPerTrk
    struct.pack_into('<H', bs, 26, 255)                     # NumHeads
    struct.pack_into('<I', bs, 28, 0)                       # HiddSec
    struct.pack_into('<I', bs, 32, total_sectors)           # TotSec32

    # FAT32 specific
    struct.pack_into('<I', bs, 36, fat_size_sectors)        # FATSz32
    struct.pack_into('<H', bs, 40, 0)                       # ExtFlags
    struct.pack_into('<H', bs, 42, 0)                       # FSVer
    struct.pack_into('<I', bs, 44, 2)                       # RootClus (cluster 2)
    struct.pack_into('<H', bs, 48, 1)                       # FSInfo sector
    struct.pack_into('<H', bs, 50, 6)                       # BkBootSec
    # bs[52:64] reserved, already zero

    # Extended boot record
    bs[64] = 0x80                                           # DrvNum
    bs[65] = 0                                              # Reserved1
    bs[66] = 0x29                                           # BootSig
    struct.pack_into('<I', bs, 67, 0x12345678)              # VolID
    label = volume_label.encode('ascii')[:11].ljust(11)
    bs[71:82] = label                                       # VolLab
    bs[82:90] = b'FAT32   '                                 # FilSysType

    # Boot signature
    bs[510] = 0x55
    bs[511] = 0xAA

    return bytes(bs)


def _build_fsinfo(free_clusters, next_free=3):
    """Build FSInfo sector (512 bytes)."""
    fs = bytearray(512)
    struct.pack_into('<I', fs, 0, 0x41615252)       # LeadSig
    # fs[4:484] reserved
    struct.pack_into('<I', fs, 484, 0x61417272)     # StrucSig
    struct.pack_into('<I', fs, 488, free_clusters)  # Free_Count
    struct.pack_into('<I', fs, 492, next_free)      # Nxt_Free
    # fs[496:508] reserved
    struct.pack_into('<H', fs, 510, 0xAA55)         # TrailSig
    return bytes(fs)


def _build_fat(fat_size_sectors, total_data_clusters):
    """Build the initial FAT table. Only the first few entries matter;
    rest is all zeros (free)."""
    fat = bytearray(fat_size_sectors * 512)
    # Entry 0: media byte + 0xFFFFF00
    struct.pack_into('<I', fat, 0, 0x0FFFFFF8)
    # Entry 1: end of chain marker
    struct.pack_into('<I', fat, 4, 0x0FFFFFFF)
    # Entry 2: root directory cluster — end of chain
    struct.pack_into('<I', fat, 8, 0x0FFFFFFF)
    return bytes(fat)


def _build_root_dir(volume_label="SWITCH"):
    """Build the root directory cluster with a volume label entry."""
    entry = bytearray(32)
    label = volume_label.encode('ascii')[:11].ljust(11)
    entry[0:11] = label
    entry[11] = 0x08  # ATTR_VOLUME_ID
    return bytes(entry) + b'\x00' * (512 * 128 - 32)  # pad to 64KB (128 sectors)


def format_fat32(drive_letter, cluster_size_kb=64, label="SWITCH",
                  progress_fn=None):
    """Format a drive as FAT32.

    Parameters
    ----------
    drive_letter : str
        Single letter, e.g. 'D'
    cluster_size_kb : int
        Cluster size in KB (default 64, recommended for Switch)
    label : str
        Volume label (max 11 chars)
    progress_fn : callable or None
        Called with progress strings: progress_fn("message")

    Returns True on success, raises on failure.
    """
    def _log(msg):
        if progress_fn:
            progress_fn(msg)

    drive_letter = drive_letter.strip().upper()[0]
    bytes_per_sector = 512
    sectors_per_cluster = (cluster_size_kb * 1024) // bytes_per_sector  # 128 for 64KB

    _log(f"Opening volume {drive_letter}:…")
    handle = _open_volume(drive_letter)

    try:
        # Lock and dismount the volume
        _log("Locking volume…")
        try:
            _ioctl(handle, FSCTL_LOCK_VOLUME)
        except OSError:
            _log("Warning: could not lock volume (may be in use)")

        _log("Dismounting volume…")
        try:
            _ioctl(handle, FSCTL_DISMOUNT_VOLUME)
        except OSError:
            pass

        # Get volume size
        total_bytes = _get_volume_size(handle)
        total_sectors = total_bytes // bytes_per_sector
        _log(f"Volume size: {total_bytes / (1024**3):.1f} GB "
             f"({total_sectors} sectors)")

        # Calculate FAT32 parameters
        reserved_sectors = 32
        num_fats = 2

        # Calculate FAT size
        # Each FAT entry is 4 bytes. We need enough entries for all data clusters.
        # data_sectors = total_sectors - reserved - (num_fats * fat_size)
        # data_clusters = data_sectors / sectors_per_cluster
        # fat_entries = data_clusters + 2  (entries 0 and 1 are reserved)
        # fat_bytes = fat_entries * 4
        # fat_sectors = ceil(fat_bytes / 512)
        #
        # Solve: fat_size = ceil((total_sectors - reserved) * 4 /
        #                        (sectors_per_cluster * 512 + num_fats * 4))
        numerator = total_sectors - reserved_sectors
        denominator = (sectors_per_cluster * bytes_per_sector // 4) + num_fats
        fat_size_sectors = (numerator + denominator - 1) // denominator

        data_start = reserved_sectors + num_fats * fat_size_sectors
        data_sectors = total_sectors - data_start
        total_data_clusters = data_sectors // sectors_per_cluster
        free_clusters = total_data_clusters - 1  # minus root dir cluster

        _log(f"Cluster size: {cluster_size_kb} KB "
             f"({sectors_per_cluster} sectors)")
        _log(f"FAT size: {fat_size_sectors} sectors "
             f"({fat_size_sectors * 512 / (1024*1024):.1f} MB)")
        _log(f"Data clusters: {total_data_clusters}")
        _log(f"Data start sector: {data_start}")

        # Sanity check — FAT32 requires at least 65525 clusters
        if total_data_clusters < 65525:
            raise ValueError(
                f"Only {total_data_clusters} clusters — too few for FAT32. "
                f"Try a smaller cluster size.")

        # Build structures
        _log("Writing boot sector…")
        boot = _build_boot_sector(
            total_sectors, sectors_per_cluster, reserved_sectors,
            num_fats, fat_size_sectors, label)
        _write_at(handle, 0, boot)

        _log("Writing FSInfo…")
        fsinfo = _build_fsinfo(free_clusters)
        _write_at(handle, 1 * bytes_per_sector, fsinfo)

        # Sector 2: empty (reserved)
        _write_at(handle, 2 * bytes_per_sector, b'\x00' * bytes_per_sector)

        # Backup boot sector at sector 6
        _log("Writing backup boot sector…")
        _write_at(handle, 6 * bytes_per_sector, boot)
        _write_at(handle, 7 * bytes_per_sector, fsinfo)
        _write_at(handle, 8 * bytes_per_sector, b'\x00' * bytes_per_sector)

        # Zero remaining reserved sectors (3-5, 9-31)
        zero_sector = b'\x00' * bytes_per_sector
        for s in list(range(3, 6)) + list(range(9, reserved_sectors)):
            _write_at(handle, s * bytes_per_sector, zero_sector)

        # Write FAT tables
        fat = _build_fat(fat_size_sectors, total_data_clusters)

        # Write in chunks to avoid huge memory allocation
        chunk_size = 1024 * 1024  # 1 MB at a time
        for fat_num in range(num_fats):
            fat_offset = (reserved_sectors + fat_num * fat_size_sectors) * bytes_per_sector
            _log(f"Writing FAT {fat_num + 1}… "
                 f"({fat_size_sectors * 512 / (1024*1024):.0f} MB)")

            # First chunk has the initialized entries
            first_chunk = min(chunk_size, len(fat))
            _write_at(handle, fat_offset, fat[:first_chunk])

            # Remaining chunks are all zeros
            remaining = fat_size_sectors * bytes_per_sector - first_chunk
            offset = fat_offset + first_chunk
            zero_chunk = b'\x00' * chunk_size
            while remaining > 0:
                write_size = min(chunk_size, remaining)
                _write_at(handle, offset, zero_chunk[:write_size])
                offset += write_size
                remaining -= write_size

        # Write root directory cluster
        _log("Writing root directory…")
        root_dir_offset = data_start * bytes_per_sector
        root_dir = _build_root_dir(label)
        _write_at(handle, root_dir_offset, root_dir)

        # Unlock
        _log("Unlocking volume…")
        try:
            _ioctl(handle, FSCTL_UNLOCK_VOLUME)
        except OSError:
            pass

    finally:
        kernel32.CloseHandle(handle)

    _log(f"✓ Formatted {drive_letter}: as FAT32 "
         f"({total_bytes / (1024**3):.1f} GB, "
         f"{cluster_size_kb}K clusters)")
    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <drive_letter>")
        print(f"Example: python {sys.argv[0]} D")
        sys.exit(1)

    letter = sys.argv[1].strip().upper()[0]
    confirm = input(f"Format {letter}: as FAT32? ALL DATA WILL BE LOST. (y/N): ")
    if confirm.strip().lower() != 'y':
        print("Cancelled.")
        sys.exit(0)

    format_fat32(letter, progress_fn=print)
