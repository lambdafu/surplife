"""Waveform generators for the Surplife display.

Each generator produces 96 bytes (one per column, values 0-100)
suitable for send_waveform().
"""

from __future__ import annotations

import math
import random
import time


def sine(freq: float = 1.0, phase: float = 0.0, amplitude: int = 80) -> bytes:
    """Sine wave across 96 columns."""
    data = bytearray(96)
    for i in range(96):
        v = math.sin(2 * math.pi * freq * i / 96 + phase)
        data[i] = max(0, min(100, int((v + 1) / 2 * amplitude)))
    return bytes(data)


def sawtooth(phase: float = 0.0, amplitude: int = 80) -> bytes:
    """Sawtooth wave across 96 columns."""
    data = bytearray(96)
    for i in range(96):
        v = ((i / 96 + phase) % 1.0)
        data[i] = max(0, min(100, int(v * amplitude)))
    return bytes(data)


def triangle(phase: float = 0.0, amplitude: int = 80) -> bytes:
    """Triangle wave across 96 columns."""
    data = bytearray(96)
    for i in range(96):
        v = ((i / 96 + phase) % 1.0)
        v = 2 * v if v < 0.5 else 2 * (1 - v)
        data[i] = max(0, min(100, int(v * amplitude)))
    return bytes(data)


def pulse(position: float = 0.5, width: float = 0.1, amplitude: int = 100) -> bytes:
    """A single pulse/bump at a position (0-1) with given width."""
    data = bytearray(96)
    center = position * 96
    w = max(1, width * 96)
    for i in range(96):
        dist = abs(i - center)
        if dist < w:
            v = math.cos(dist / w * math.pi / 2)
            data[i] = max(0, min(100, int(v * v * amplitude)))
    return bytes(data)


def noise(amplitude: int = 80) -> bytes:
    """White noise — random values each column."""
    return bytes(max(0, min(100, random.randint(0, amplitude))) for _ in range(96))


def perlin_noise(phase: float = 0.0, scale: float = 0.1, amplitude: int = 80) -> bytes:
    """Smooth random noise using interpolated random values."""
    # Simple interpolated noise (not true Perlin, but smooth enough)
    n_points = 12
    anchors = [random.random() for _ in range(n_points + 1)]
    # Use phase to seed consistently per call
    random.seed(int(phase * 1000) % 10000)
    anchors = [random.random() for _ in range(n_points + 1)]
    random.seed()  # restore randomness

    data = bytearray(96)
    for i in range(96):
        t = i / 96 * n_points
        idx = int(t)
        frac = t - idx
        # Cosine interpolation
        v = anchors[idx] * (1 - frac) + anchors[min(idx + 1, n_points)] * frac
        data[i] = max(0, min(100, int(v * amplitude)))
    return bytes(data)


def vu_meter(level: float, peak: float | None = None) -> bytes:
    """VU meter style — filled bar from left to level (0-1), optional peak marker."""
    data = bytearray(96)
    fill = int(level * 96)
    for i in range(min(fill, 96)):
        data[i] = 100
    if peak is not None:
        pos = int(peak * 96)
        if 0 <= pos < 96:
            data[pos] = 100
    return bytes(data)


def flat(level: int = 50) -> bytes:
    """All columns at the same level."""
    return bytes([max(0, min(100, level))] * 96)


def from_bytes(data: bytes, offset: int = 0) -> bytes:
    """Extract 96 amplitude values from raw byte data.

    Each byte is clamped to 0-100. Useful for streaming arbitrary
    file contents as waveform data.
    """
    chunk = data[offset:offset + 96]
    if len(chunk) < 96:
        chunk = chunk + b'\x00' * (96 - len(chunk))
    return bytes(max(0, min(100, b)) for b in chunk)


# ── Animated generators (return frames over time) ────────────────

def animate_sine(freq: float = 2.0, speed: float = 1.0) -> bytes:
    """Sine wave that scrolls over time."""
    phase = time.monotonic() * speed
    return sine(freq=freq, phase=phase)


def animate_pulse(speed: float = 1.0, width: float = 0.15) -> bytes:
    """Pulse that bounces left to right."""
    t = time.monotonic() * speed
    pos = (math.sin(t) + 1) / 2
    return pulse(position=pos, width=width)


def animate_noise(amplitude: int = 80) -> bytes:
    """Fresh noise each frame."""
    return noise(amplitude=amplitude)


def animate_rain(speed: float = 1.0) -> bytes:
    """Smooth random waves that drift over time."""
    phase = time.monotonic() * speed
    return perlin_noise(phase=phase, amplitude=80)
