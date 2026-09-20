"""Command-line interface for Surplife displays."""

from __future__ import annotations

import asyncio
import logging

import click

from . import __version__
from .display import PLAYLIST_DEFAULT_DURATION, SurplifeDisplay
from .scanner import discover


@click.group(invoke_without_command=True)
@click.version_option(__version__)
@click.option("-v", "--verbose", count=True, help="Increase verbosity (-v debug).")
@click.option("-d", "--device", default=None,
              help="BLE address or device name (e.g. IOTBTD98 or D98). "
                   "Auto-discovers nearest display if omitted.")
@click.pass_context
def cli(ctx: click.Context, verbose: int, device: str | None) -> None:
    """Control Surplife LED matrix displays over BLE."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s.%(msecs)03d %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Only increase verbosity for our own loggers, not bleak's.
    logging.getLogger("surplife").setLevel(level)
    ctx.ensure_object(dict)
    ctx.obj["device"] = device
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _get_device(ctx: click.Context) -> str | None:
    return ctx.obj["device"]


@cli.command()
@click.option("-t", "--timeout", default=10.0, help="Scan duration in seconds.")
def scan(timeout: float) -> None:
    """Scan for nearby Surplife displays."""
    asyncio.run(_scan(timeout))


async def _scan(timeout: float) -> None:
    from .scanner import discover_live

    found: list = []
    total_seen = 0

    def on_update(surplife_devices, total):
        nonlocal found, total_seen
        found = surplife_devices
        total_seen = total
        _scan_status(len(found), total_seen, timeout, done=False)

    _scan_status(0, 0, timeout, done=False)
    found = await discover_live(
        timeout=timeout, on_update=on_update,
    )
    _scan_status(len(found), total_seen, timeout, done=True)

    if not found:
        click.echo("No Surplife displays found.")
        return
    click.echo(f"Found {len(found)} display(s):\n")
    for d in found:
        click.echo(f"  {d.name:15s}  {d.address}  RSSI={d.rssi}")


def _scan_status(surplife: int, total: int, timeout: float, done: bool) -> None:
    spinner = "/-\\|"
    if done:
        click.echo("\r" + " " * 60 + "\r", nl=False)
    else:
        import time
        frame = spinner[int(time.monotonic() * 4) % len(spinner)]
        click.echo(
            f"\r{frame} Scanning... {surplife} display(s) found ({total} BLE total) ",
            nl=False,
        )


@cli.command()
@click.argument("state", type=click.Choice(["on", "off", "true", "false"], case_sensitive=False))
@click.pass_context
def power(ctx: click.Context, state: str) -> None:
    """Set display power (on/off)."""
    on = state in ("on", "true")
    asyncio.run(_run(ctx, lambda d: d.power_on() if on else d.power_off()))


@cli.command()
@click.argument("value", type=click.IntRange(0, 100))
@click.pass_context
def brightness(ctx: click.Context, value: int) -> None:
    """Set display brightness (0-100)."""
    asyncio.run(_run(ctx, lambda d: d.set_brightness(value)))


@cli.command()
@click.argument("value", type=click.IntRange(1, 100))
@click.pass_context
def speed(ctx: click.Context, value: int) -> None:
    """Set animation speed (1-100)."""
    asyncio.run(_run(ctx, lambda d: d.set_speed(value)))


@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("-e", "--effect", default=1, type=click.IntRange(1, 10),
              help="Display effect (1=static, 2=scroll-r, ...)")
@click.option("-s", "--speed", default=50, type=click.IntRange(1, 100),
              help="Animation speed.")
@click.option("--force", is_flag=True, help="Bypass device cache.")
@click.pass_context
def image(ctx: click.Context, file: str, effect: int, speed: int, force: bool) -> None:
    """Upload and display an image file."""
    asyncio.run(_run(ctx, lambda d: d.show_image_file(file, effect=effect, speed=speed, force=force)))


@cli.command()
@click.argument("file", type=click.Path(exists=True))
@click.option("-s", "--speed", default=50, type=click.IntRange(1, 100),
              help="Animation speed.")
@click.option("--force", is_flag=True, help="Bypass device cache.")
@click.pass_context
def gif(ctx: click.Context, file: str, speed: int, force: bool) -> None:
    """Upload and play a GIF animation."""
    asyncio.run(_run(ctx, lambda d: d.show_gif_file(file, speed=speed, force=force)))


@cli.command()
@click.argument("message")
@click.option("-c", "--color", multiple=True,
              help="Text color as hex RGB (e.g. ff0000). Repeat for gradient.")
@click.option("-s", "--speed", default=50, type=click.IntRange(1, 100),
              help="Scroll speed.")
@click.option("--font-size", default=16, type=int, help="Font size in pixels.")
@click.option("--force", is_flag=True, help="Bypass device cache.")
@click.pass_context
def text(ctx: click.Context, message: str, color: tuple[str, ...],
         speed: int, font_size: int, force: bool) -> None:
    """Upload and display scrolling text."""
    from .color import parse_rgb_hex
    colors = [parse_rgb_hex(c) for c in color] if color else None
    asyncio.run(_run(ctx, lambda d: d.show_text(
        message, colors=colors, speed=speed, font_size=font_size, force=force)))


@cli.command()
@click.argument("style", type=click.IntRange(0, 7), default=0)
@click.option("--12h", "hour_12", is_flag=True, help="Use 12-hour format.")
@click.option("--date", "show_date", is_flag=True, help="Show date.")
@click.pass_context
def clock(ctx: click.Context, style: int, hour_12: bool, show_date: bool) -> None:
    """Show firmware clock (style 0-7)."""
    asyncio.run(_run(ctx, lambda d: d.show_clock(style, not hour_12, show_date)))


@cli.group()
def playlist() -> None:
    """Manage the device playlist (carousel of cached content)."""


@playlist.command("list")
@click.pass_context
def playlist_list(ctx: click.Context) -> None:
    """Show the playlist."""
    async def action(d):
        entries = await d.get_playlist()
        if not entries:
            click.echo("Playlist is empty.")
        for e in entries:
            pos = f"#{e.index}" if e.index else "--"
            click.echo(f"  {pos:3s}  {e.hash.hex()}  {e.duration:3d}s  flag={e.flag:02x}")
    asyncio.run(_run(ctx, action))


@playlist.command("add")
@click.argument("c_hash", metavar="HASH", callback=lambda _c, _p, v: _parse_hash(v))
@click.option("-t", "--duration", default=PLAYLIST_DEFAULT_DURATION,
              type=click.IntRange(1, 255), help="Display time in seconds.")
@click.pass_context
def playlist_add(ctx: click.Context, c_hash: bytes, duration: int) -> None:
    """Add uploaded content (by hash) to the playlist."""
    asyncio.run(_run(ctx, lambda d: d.playlist_add(c_hash, duration=duration)))


@playlist.command("rm")
@click.argument("c_hash", metavar="HASH", callback=lambda _c, _p, v: _parse_hash(v))
@click.pass_context
def playlist_rm(ctx: click.Context, c_hash: bytes) -> None:
    """Remove content (by hash) from the playlist."""
    asyncio.run(_run(ctx, lambda d: d.playlist_remove(c_hash)))


def _parse_hash(text: str) -> bytes:
    try:
        h = bytes.fromhex(text)
    except ValueError:
        raise click.BadParameter("not a hex string")
    if len(h) != 16:
        raise click.BadParameter("must be 32 hex digits")
    return h


@cli.command()
@click.pass_context
def shell(ctx: click.Context) -> None:
    """Interactive shell with multi-device support."""
    from .shell import run_shell
    run_shell(initial_device=_get_device(ctx))


async def _run(ctx: click.Context, action) -> None:
    """Connect, init, run action, disconnect."""
    async with SurplifeDisplay(_get_device(ctx)) as d:
        await action(d)
