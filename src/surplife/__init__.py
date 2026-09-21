"""Surplife LED Display — BLE Control Library

Reverse-engineered protocol for Surplife 96x16 LED matrix displays.
Communicates over BLE using GATT service 0000ffff with characteristics
ff01 (write) and ff02 (notify).

Display: 96 columns x 16 rows, 2 bytes/pixel (16-bit HSV), column-major.
Pixel data uses a2pl compression (LZ77 variant).
"""

__version__ = "0.2.0"
