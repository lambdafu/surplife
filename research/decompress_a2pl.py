"""
a2pl decompressor — LZ77 variant used by the Surplife display.

Each command byte = [upper nibble: literal count] [lower nibble: backref mode].
  - Lower nibble 0x0-0xE: short backref — LE16(dist), copy = nibble + 4 bytes
  - Lower nibble 0xF: long backref — LE16(dist) + sum_encoded(N), copy = N + 19 bytes

Stream ends when compressed data runs out (no explicit terminator).
Output should be exactly 3072 bytes (96 cols × 16 rows × 2 bytes/pixel HSV).
"""

import sys
import os
import json


def sum_decode(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a sum-encoded value starting at offset.
    Returns (value, new_offset)."""
    value = 0
    while offset < len(data) and data[offset] == 0xFF:
        value += 255
        offset += 1
    if offset < len(data):
        value += data[offset]
        offset += 1
    return value, offset


def decompress_a2pl(data: bytes, verbose: bool = False) -> bytes:
    """Decompress a2pl stream to raw framebuffer bytes.

    Returns the decompressed framebuffer (should be 3072 bytes for a full frame).
    """
    fb = bytearray()
    i = 0

    while i < len(data):
        b = data[i]
        upper = (b >> 4) & 0x0F
        lower = b & 0x0F
        i += 1

        # Literal count: upper nibble, extended via sum-encoding if 0xF
        if upper == 0x0F:
            ext, i = sum_decode(data, i)
            n_colors = 15 + ext
        else:
            n_colors = upper

        color_bytes = data[i:i+n_colors]
        if verbose:
            if n_colors > 0:
                print(f'  [{i-1:3d}] LIT {b:02x}: {n_colors} bytes: {color_bytes.hex(" ")}')
            else:
                print(f'  [{i-1:3d}] LIT {b:02x}: 0 bytes')
            print(f'         FB pos {len(fb)} → {len(fb)+n_colors}')
        fb.extend(color_bytes)
        i += n_colors

        # Back-reference: LE16 distance
        if i + 1 >= len(data):
            break
        dist = data[i] | (data[i+1] << 8)
        i += 2

        if lower == 0x0F:
            # Long backref: sum-encoded length, copy = sum + 19
            length, i = sum_decode(data, i)
            copy_len = length + 19
            if verbose:
                print(f'         BACKREF dist={dist} len={length}+19={copy_len}')
        else:
            # Short backref: copy = lower_nibble + 4
            copy_len = lower + 4
            if verbose:
                print(f'         BACKREF dist={dist} copy={copy_len} (short: {lower}+4)')

        if verbose:
            print(f'         FB pos {len(fb)} → {len(fb)+copy_len}')

        # LZ77 copy: when copy_len > dist, wraps around (repeating pattern)
        start = len(fb) - dist
        if start < 0:
            if verbose:
                print(f'         WARNING: back-ref before buffer start (start={start})')
            for j in range(copy_len):
                src = start + j
                fb.append(0 if src < 0 else fb[src])
        else:
            for j in range(copy_len):
                fb.append(fb[start + (j % dist) if dist > 0 else 0])

    return bytes(fb)


def main():
    verbose = '-v' in sys.argv

    # If given hex data directly
    if '--hex' in sys.argv:
        idx = sys.argv.index('--hex')
        hex_str = sys.argv[idx + 1].replace(' ', '')
        data = bytes.fromhex(hex_str)
        fb = decompress_a2pl(data, verbose=True)
        print(f'\nDecompressed: {len(fb)} bytes (expected 3072)')
        print(f'Hex: {fb.hex(" ")[:200]}...')
        return

    # Otherwise, process trace files
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from analyze_traces import extract_uploads, parse_btsnoop, reassemble

    files = [a for a in sys.argv[1:] if not a.startswith('-')]
    if not files:
        print("Usage: python3 decompress_a2pl.py [-v] [--hex HEXDATA] [trace_files...]")
        return

    for filepath in files:
        basename = os.path.splitext(os.path.basename(filepath))[0]
        try:
            frames = parse_btsnoop(filepath)
        except (ValueError, IOError) as e:
            print(f"Skipping {filepath}: {e}")
            continue
        ops = reassemble(frames)
        uploads = extract_uploads(ops, filepath)

        for u in uploads:
            if 'pixel_data_hex' not in u:
                continue
            pix_hex = u['pixel_data_hex'].replace(' ', '')
            pix_data = bytes.fromhex(pix_hex)
            utype = u.get('type', 'upload')
            frame = u.get('frame', '?')

            if verbose:
                print(f'\n{"="*60}')
                print(f'{basename} (frame {frame}, {utype})')
                print(f'Compressed: {len(pix_data)} bytes')
                print(f'Hex: {pix_data.hex(" ")}')
                print()

            try:
                fb = decompress_a2pl(pix_data, verbose=verbose)
                status = "OK" if len(fb) == 3072 else f"WRONG SIZE"
                print(f'{basename:50s} frame={frame:>5} {utype:12s} '
                      f'compressed={len(pix_data):4d} → decompressed={len(fb):5d}  {status}')
            except Exception as e:
                print(f'{basename:50s} frame={frame:>5} {utype:12s} '
                      f'compressed={len(pix_data):4d} → ERROR: {e}')


if __name__ == "__main__":
    main()
