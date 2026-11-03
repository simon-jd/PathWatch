"""PathWatch disk share on home-ubuntu, measured on the checkout.

Doubled from 6 GB to 12 GB by the user on 2026-10-27. Docker data lives on
/mnt/dev-data and is not counted.
"""
DISK_SHARE = 12_000_000_000
SOFT_DISK = DISK_SHARE - 400_000_000  # admission forecasts stop here
RUNTIME_DISK = DISK_SHARE - 300_000_000  # running fixtures hard-stop here
