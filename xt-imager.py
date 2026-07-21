#!/usr/bin/env python3

import os
import sys
import pathlib
import re
import argparse
from typing import List
from string import printable
import gzip
import zlib
import serial


# UFS device 0 stores IPL boot data and must never be modified.
# The main UFS storage is exposed by u-boot as SCSI device 1.
UFS_DEVICE = 1


def main():
    """Main function"""

    parser = argparse.ArgumentParser(
        description='Flash image files through u-boot and tftp',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument(
        'image',
        nargs='?',
        type=pathlib.Path,
        help='Name of the image file to flash (skip for stdin)')

    # Select eMMC or UFS as the flashing target
    parser.add_argument(
        '--target',
        required=True,
        choices=('emmc', 'ufs'),
        help='Storage target to flash')

    parser.add_argument(
        '-s',
        '--serial',
        default='/dev/ttyUSB0',
        help='Serial console to use')

    parser.add_argument(
        '-b',
        '--baud',
        type=int,
        default=921600,
        help='Baudrate')

    parser.add_argument(
        '-t',
        '--tftp',
        type=pathlib.Path,
        default="/srv/tftp",
        help="Path to the TFTP directory")

    parser.add_argument(
        '--loadaddr',
        default='0x58000000',
        help='loadaddr for u-boot, 0x.. format')

    parser.add_argument(
        '--mmcdev',
        type=int,
        default=0,
        help='MMC device in u-boot')

    parser.add_argument(
        '--buffersize',
        type=int,
        default=512*1024*1024,
        help='Buffer size, 512-bytes aligned')

    parser.add_argument(
        '--serverip',
        help='IP of the host that will be used TFTP transfer.')

    parser.add_argument(
        '--ipaddr',
        help='IP of the board that will be used TFTP transfer.')

    args = parser.parse_args()

    # Ensure that chunks are valid for block-device writes
    if args.buffersize <= 0 or args.buffersize % 512 != 0:
        parser.error('--buffersize must be a positive, 512-byte aligned value')

    if not os.path.isdir(args.tftp):
        raise NotADirectoryError('-t parameter is not a directory')

    print(f'[Use {args.tftp} as a TFTP root]')
    print(f"[Reading data from {args.image if args.image else 'STDIN'}]")

    do_flash_image(args, args.tftp)


# Build the storage-specific gzwrite command
def build_write_command(args, offset):
    """Build u-boot command to write a chunk to the selected device"""

    if args.target == 'emmc':
        return (
            f'gzwrite mmc {args.mmcdev} '
            f'${{loadaddr}} ${{filesize}} 400000 {offset:X}\r')

    if args.target == 'ufs':
        return (
            f'gzwrite scsi {UFS_DEVICE} '
            f'${{loadaddr}} ${{filesize}} 400000 {offset:X}\r')

    raise ValueError(f'Unsupported flashing target: {args.target}')


# Detect compressed image files by gzip magic bytes

def is_gzip_image(image_path):
    """Check whether the image file is gzip-compressed"""

    with open(image_path, 'rb') as f_img:
        return f_img.read(2) == b'\x1f\x8b'


# Open both raw and gzip images as an uncompressed stream
def open_image_file(image_path):
    """Open raw or gzip-compressed image as an uncompressed stream"""

    if is_gzip_image(image_path):
        return gzip.open(image_path, 'rb')

    return open(image_path, 'rb')


# Calculate the uncompressed image size for validation and progress

def get_image_size(image_path):
    """Get the uncompressed image size"""

    if not is_gzip_image(image_path):
        return os.path.getsize(image_path)

    print('[Checking gzip archive and measuring uncompressed image]')
    image_size = 0

    with gzip.open(image_path, 'rb') as f_img:
        while True:
            data = f_img.read(64*1024*1024)

            if not data:
                break

            image_size += len(data)

    return image_size


# Check the image before connecting to and modifying the UFS device
def validate_ufs_image(args):
    """Validate image file used for UFS flashing"""

    if args.image is None or str(args.image) == '-':
        raise ValueError(
            'UFS flashing requires an image file; stdin is not supported')

    if not args.image.exists():
        raise FileNotFoundError(f'UFS image does not exist: {args.image}')

    if not args.image.is_file():
        raise ValueError(f'UFS image is not a regular file: {args.image}')

    # Warn about an unusual filename without blocking the operation
    if 'ufs' not in args.image.name.lower():
        print(f"[WARNING: image name does not contain 'ufs': {args.image.name}]")

    image_size = get_image_size(args.image)

    if image_size == 0:
        raise ValueError(f'UFS image is empty: {args.image}')

    return image_size


# Verify that the image fits the UFS device and matches its block size
def validate_ufs_image_size(image_size, block_count, block_size):
    """Check that the image fits the UFS device and is block-aligned"""

    capacity_bytes = block_count * block_size

    if image_size > capacity_bytes:
        raise ValueError(
            f'UFS image is too large: {image_size / 1024**3:.2f} GiB; '
            f'device capacity is {capacity_bytes / 1024**3:.2f} GiB')

    if image_size % block_size != 0:
        raise ValueError(
            f'UFS image size {image_size} is not aligned '
            f'to the device block size {block_size}')

    print(
        f'[UFS image size: {image_size / 1024**3:.2f} GiB; '
        f'device capacity: {capacity_bytes / 1024**3:.2f} GiB]')


# Require explicit confirmation before the destructive UFS operation
def confirm_ufs_flash(args, image_size, block_count, block_size):
    """Ask user to confirm destructive UFS flashing"""

    capacity_bytes = block_count * block_size
    confirmation_text = f'FLASH UFS {UFS_DEVICE}'

    print('')
    print('[WARNING: destructive UFS flashing operation]')
    print('[UFS device 0 is reserved for IPL booting and will not be modified.]')
    print(f'[Image: {args.image}]')
    print(f'[Image size: {image_size / 1024**3:.2f} GiB]')
    print(
        f'[Target: SCSI/UFS device {UFS_DEVICE}, '
        f'{capacity_bytes / 1024**3:.2f} GiB]')
    print(
        f'[All existing data on UFS device '
        f'{UFS_DEVICE} may be destroyed.]')
    print(f'[To continue, type exactly: {confirmation_text}]')

    try:
        answer = input('> ')
    except (EOFError, KeyboardInterrupt) as error:
        raise RuntimeError(
            'UFS flashing confirmation was cancelled') from error

    if answer != confirmation_text:
        raise RuntimeError(
            'UFS flashing confirmation did not match; '
            'flashing was not started')


# Parse UFS capacity information from the u-boot SCSI output

def get_scsi_device_capacity(output, device):
    """Get block count and block size for a SCSI device"""

    device_pattern = (
        rf'(?ms)^[ \t]*Device\s+{device}:'
        rf'.*?'
        rf'(?=^[ \t]*Device\s+\d+:|\Z)')

    device_match = re.search(device_pattern, output)

    if not device_match:
        return None

    capacity_match = re.search(
        r'Capacity:.*?\((\d+)\s+x\s+(\d+)\)',
        device_match.group(0),
        re.DOTALL)

    if not capacity_match:
        return None

    return int(capacity_match.group(1)), int(capacity_match.group(2))


# Scan, validate and select the UFS device before flashing
def prepare_ufs_target(conn, uboot_prompt):
    """Detect and select UFS device"""

    if UFS_DEVICE != 1:
        raise RuntimeError(
            'Only UFS device 1 is allowed for flashing; '
            'UFS device 0 is reserved for IPL booting')

    conn_send(conn, 'scsi scan\r')
    scan_output = conn_wait_for_any(conn, [uboot_prompt])
    capacity = get_scsi_device_capacity(scan_output, UFS_DEVICE)

    if capacity is None:
        raise RuntimeError(
            f'Could not determine capacity of UFS device {UFS_DEVICE}')

    block_count, block_size = capacity
    capacity_bytes = block_count * block_size

    if block_size not in (512, 4096):
        raise RuntimeError(
            f'Refusing to use UFS device {UFS_DEVICE}: '
            f'unexpected block size {block_size}')

    conn_send(conn, f'scsi device {UFS_DEVICE}\r')
    select_output = conn_wait_for_any(conn, [uboot_prompt])

    # Verify that the expected UFS device is now active
    selection_succeeded = (
        f'Device {UFS_DEVICE}:' in select_output and
        'is now current device' in select_output)

    if not selection_succeeded:
        raise RuntimeError(
            f'Could not confirm selection of UFS device {UFS_DEVICE}')

    print(
        f'\n[Selected UFS device {UFS_DEVICE}: '
        f'{capacity_bytes / 1024**3:.2f} GiB, '
        f'block size {block_size}]')

    return block_count, block_size


def do_flash_image(args, tftp_root):
    """Flash image to the selected storage device"""

    image_size = validate_ufs_image(args) if args.target == 'ufs' else None

    conn = serial.Serial(port=args.serial, baudrate=args.baud, timeout=20)

    f_img = None
    out_fullname = None

    try:
        uboot_prompt = '=>'
        # Send 'CR', and check for one of the possible options:
        # - uboot_prompt appears, if u-boot console is already active
        # - u-boot is just starting, so we will get "Hit any key.."
        print('[Waiting for u-boot prompt...]')
        conn_send(conn, '\r')
        conn_wait_for_any(
            conn,
            [uboot_prompt, 'Hit any key to stop autoboot:'])
        # In case we got "Hit any key", let's stop the boot
        conn_send(conn, '\r')
        conn_wait_for_any(conn, [uboot_prompt])
        print('\n[Connected to u-boot]')

        # UFS requires device detection and additional safety checks
        if args.target == 'ufs':
            block_count, block_size = prepare_ufs_target(
                conn, uboot_prompt)

            # UFS chunks must be aligned to the reported block size
            if args.buffersize % block_size != 0:
                raise ValueError(
                    f'Buffer size {args.buffersize} is not aligned '
                    f'to UFS block size {block_size}')

            # Make sure that the selected image can be written safely
            validate_ufs_image_size(
                image_size, block_count, block_size)
            # Ask for confirmation after the exact target is known
            confirm_ufs_flash(
                args, image_size, block_count, block_size)

        # Open input file or stdin
        if args.image and str(args.image) != '-':
            if image_size is None:
                image_size = get_image_size(args.image)

            # Transparently decompress gzip images while reading
            f_img = open_image_file(args.image)
        else:
            f_img = sys.stdin.buffer
            image_size = None

        chunk_filename = 'chunk.bin.gz'
        chunk_size_in_bytes = args.buffersize

        bytes_sent = 0
        out_fullname = os.path.join(tftp_root, chunk_filename)

        if args.serverip:
            conn_send(conn, f'env set serverip {args.serverip}\r')
            conn_wait_for_any(conn, [uboot_prompt])

        if args.ipaddr:
            conn_send(conn, f'env set ipaddr {args.ipaddr}\r')
            conn_wait_for_any(conn, [uboot_prompt])

        conn_send(conn, f'env set loadaddr {args.loadaddr}\r')
        conn_wait_for_any(conn, [uboot_prompt])
        print('')

        try:
            # do in loop:
            # - read X MB chunk from image file
            # - save chunk to file in tftp root
            # - tell u-boot to write chunk to the selected storage device
            while True:
                print('[Reading chunk]')
                data = f_img.read(chunk_size_in_bytes)

                if not data:
                    break

                computed_crc = zlib.crc32(data) & 0xffffffff

                # create chunk
                print('[Compressing chunk]')
                data_packed = gzip.compress(data, compresslevel=1)

                with open(out_fullname, 'wb') as f_out:
                    f_out.write(data_packed)

                conn_send(conn, f'tftp ${{loadaddr}} {chunk_filename}\r')
                # check that all bytes are transmitted
                conn_wait_for_any(
                    conn,
                    [f'Bytes transferred = {len(data_packed)}'])
                conn_wait_for_any(conn, [uboot_prompt])

                # write to the selected storage device
                # Use either gzwrite mmc or gzwrite scsi
                conn_send(conn, build_write_command(args, bytes_sent))
                conn_wait_for_any(
                    conn,
                    [f'{len(data)} bytes, crc 0x{computed_crc:08x}'])
                print('  [CRC is OK]')
                conn_wait_for_any(conn, [uboot_prompt])

                bytes_sent += len(data)

                if image_size:
                    print(
                        f'\n[Progress: {bytes_sent:_}/{image_size:_} '
                        f'({bytes_sent*100 // image_size}%)]')
                else:
                    print(f'\n[Progress: {bytes_sent:_}]')
        finally:
            # remove chunk from tftp root
            if out_fullname is not None and os.path.exists(out_fullname):
                os.remove(out_fullname)
    finally:
        if f_img is not None and f_img is not sys.stdin.buffer:
            f_img.close()

        conn.close()

    print("[Image was flashed successfully]")


def conn_wait_for_any(conn, expect: List[str]):
    """ Wait for any of the expected response from u-boot"""

    rcv_str = ""
    # stay in the read loop until any of expected string is received
    # in other words - all expected substrings are not in received buffer
    while all([x not in rcv_str for x in expect]):
        data = conn.read(1)

        if not data:
            raise TimeoutError(
                f'Timeout waiting for `{expect}` from the device')

        rcv_char = chr(data[0])

        if rcv_char in printable or rcv_char == '\b':
            print(rcv_char, end='', flush=True)

        rcv_str += rcv_char

    # Return captured output for parsing SCSI device information
    return rcv_str


def conn_send(conn, data):
    """ Send the string to the u-boot"""

    conn.write(data.encode('ascii'))


if __name__ == "__main__":
    main()
