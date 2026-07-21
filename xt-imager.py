#!/usr/bin/env python3

import os
import sys
import pathlib
import argparse
from typing import List
from string import printable
import gzip
import zlib
import serial


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

    parser.add_argument(
        '-s',
        '--serial',
        default='/dev/ttyUSB0',
        help='Serial console to use')

    parser.add_argument(
        '-b',
        '--baud',
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
        default=0,
        help='MMC device in u-boot')

    parser.add_argument(
        '--buffersize',
        default=512*1024*1024,
        help='Buffer size, 512-bytes aligned')

    parser.add_argument(
        '--serverip',
        help='IP of the host that will be used TFTP transfer.')

    parser.add_argument(
        '--ipaddr',
        help='IP of the board that will be used TFTP transfer.')

    args = parser.parse_args()

    if not os.path.isdir(args.tftp):
        raise NotADirectoryError("-t parameter is not a directory")

    print(f"[Use {args.tftp} as a TFTP root]")

    print(f"[Reading data from {args.image if args.image else 'STDIN'}]")

    do_flash_image(args, args.tftp)


def do_flash_image(args, tftp_root):
    """Flash image to the eMMC"""

    conn = serial.Serial(port=args.serial, baudrate=args.baud, timeout=20)

    uboot_prompt = "=>"
    # Send 'CR', and check for one of the possible options:
    # - uboot_prompt appears, if u-boot console is already active
    # - u-boot is just starting, so we will get "Hit any key.."
    print("[Waiting for u-boot prompt...]")
    conn_send(conn, "\r")
    conn_wait_for_any(conn, [uboot_prompt, "Hit any key to stop autoboot:"])
    # In case we got "Hit any key", let's stop the boot
    conn_send(conn, "\r")
    conn_wait_for_any(conn, [uboot_prompt])
    print("\n[Connected to u-boot]")

    # Open input file or stdin
    if args.image and str(args.image) != "-":
        f_img = open(args.image, "rb")
        image_size = os.path.getsize(args.image)
    else:
        f_img = sys.stdin.buffer
        image_size = None

    chunk_filename = "chunk.bin.gz"
    chunk_size_in_bytes = args.buffersize

    bytes_sent = 0
    out_fullname = os.path.join(tftp_root, chunk_filename)

    if args.serverip:
        conn_send(conn, f"env set serverip {args.serverip}\r")
        conn_wait_for_any(conn, [uboot_prompt])

    if args.ipaddr:
        conn_send(conn, f"env set ipaddr {args.ipaddr}\r")
        conn_wait_for_any(conn, [uboot_prompt])

    conn_send(conn, f"env set loadaddr {args.loadaddr}\r")
    conn_wait_for_any(conn, [uboot_prompt])
    print("")

    try:
        # do in loop:
        # - read X MB chunk from image file
        # - save chunk to file in tftp root
        # - tell u-boot to 'tftp-and-emmc' chunk
        while True:
            print("[Reading chunk]")
            data = f_img.read(chunk_size_in_bytes)

            if not data:
                break

            computed_crc = zlib.crc32(data) & 0xffffffff

            # create chunk
            print("[Compressing chunk]")
            f_out = open(out_fullname, "wb")
            data_packed = gzip.compress(data, compresslevel=1)
            f_out.write(data_packed)
            f_out.close()
            conn_send(conn, f"tftp ${{loadaddr}} {chunk_filename}\r")
            # check that all bytes are transmitted
            conn_wait_for_any(conn, [f"Bytes transferred = {len(data_packed)}"])
            conn_wait_for_any(conn, [uboot_prompt])
            # write to eMMC
            conn_send(conn, f"gzwrite mmc {args.mmcdev} ${{loadaddr}} ${{filesize}} 400000 {bytes_sent:X}\r")
            conn_wait_for_any(conn, [f"{len(data)} bytes, crc 0x{computed_crc:08x}"])
            print("  [CRC is OK]")
            conn_wait_for_any(conn, [uboot_prompt])

            bytes_sent += len(data)

            if image_size:
                print(f"\n[Progress: {bytes_sent:_}/{image_size:_} ({bytes_sent*100 // image_size}%)]")
            else:
                print(f"\n[Progress: {bytes_sent:_}]")
    finally:
        # remove chunk from tftp root
        os.remove(out_fullname)

    if f_img != sys.stdin.buffer:
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
            raise TimeoutError(f"Timeout waiting for `{expect}` from the device")
        rcv_char = chr(data[0])
        if (rcv_char in printable or rcv_char == '\b'):
            print(rcv_char, end='', flush=True)
        rcv_str += rcv_char


def conn_send(conn, data):
    """ Send the string to the u-boot"""
    conn.write(data.encode("ascii"))


if __name__ == "__main__":
    main()
