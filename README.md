# xt-imager

`xt-imager` flashes full system images through the U-Boot serial console.
The script supports both eMMC and UFS targets and automatically transfers
compressed chunks over TFTP before writing them to the selected storage.


## Requirements

- A board with a working U-Boot console.
- TFTP server accessible by U-Boot.
- A serial connection to a board running U-Boot.
- A raw (`.img`) or gzip-compressed (`.img.gz`) image.

For a complete list of command-line options and their descriptions, run:

```sh
./xt-imager.py --help
```

## Flashing an eMMC image

1. Turn on the board and boot to the U-Boot console.
2. Run the script:

```
zcat full.img.gz | ./xt-imager.py --target emmc -s /dev/GEN5_CONSOLE3 -b 1843200
```

3. Wait for the flashing process to complete.

Progress is printed after every 512 MiB written.

## Flashing a UFS image

> [!WARNING]
> Flashing overwrites data on the selected storage device.
>
> When flashing a UFS image, UFS device 0 must never be overwritten.
> It contains the IPL boot configuration. To protect this configuration,
> the script intentionally flashes only UFS device 1, which is the user
> storage passed to DomD.

1. Turn on the board and boot to the U-Boot console.
2. Run the script:

```
./xt-imager.py --target ufs full_ufs.img.gz -s /dev/GEN5_CONSOLE3 -b 1843200
```

3. Wait for the flashing process to complete.

Before writing, the script:

- selects the UFS user storage device;
- verifies that the image fits on the target device;
- verifies block-size alignment;
- requires explicit confirmation before starting the destructive operation.

Progress is printed after every 512 MiB written.
