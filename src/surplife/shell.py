"""Interactive shell for controlling Surplife displays."""

from __future__ import annotations

import asyncio
import glob
import os
import readline
import signal
import shlex
import time

from .display import PLAYLIST_DEFAULT_DURATION, SurplifeDisplay

# Effect names for tab completion and help
EFFECTS = {
    "static": 1, "scroll-r": 2, "scroll-l": 3,
    "flicker": 4, "breathe": 5, "snowflake": 6,
    "blend": 7, "sweep": 8, "bands": 9, "wipe": 10,
}


class Shell:
    """Multi-device interactive shell.

    Maintains a numbered list of connected displays. One is "active"
    at any time and receives commands.
    """

    def __init__(self) -> None:
        self._displays: list[SurplifeDisplay] = []
        self._names: list[str] = []
        self._active: int = -1
        # Last content hash uploaded per display, for "playlist-add last".
        self._last_hash: dict[SurplifeDisplay, bytes] = {}

    @property
    def active(self) -> SurplifeDisplay | None:
        if 0 <= self._active < len(self._displays):
            return self._displays[self._active]
        return None

    def _require_active(self) -> SurplifeDisplay:
        d = self.active
        if d is None:
            raise ShellError("No device connected. Use: connect <device>")
        return d

    def run_sync(self, loop: asyncio.AbstractEventLoop,
                  initial_device: str | None = None) -> None:
        """Run the shell with a synchronous input loop.

        Uses the provided event loop for async commands, but reads input
        synchronously so Ctrl-C/Ctrl-D work naturally.
        """
        print("Surplife interactive shell. Type 'help' for commands, 'quit' to exit.")
        if initial_device:
            loop.run_until_complete(self._cmd_connect([initial_device]))

        self._setup_readline()

        try:
            while True:
                try:
                    line = input(self._build_prompt())
                except EOFError:
                    break
                except KeyboardInterrupt:
                    print()
                    break

                line = line.strip()
                if not line:
                    continue
                try:
                    loop.run_until_complete(self._dispatch(line))
                except _Quit:
                    break
                except KeyboardInterrupt:
                    print()  # Ctrl-C during a command (e.g. wave)
                except ShellError as e:
                    print(f"Error: {e}")
                except Exception as e:
                    print(f"Error: {type(e).__name__}: {e}")
        finally:
            loop.run_until_complete(self._disconnect_all())

    def _setup_readline(self) -> None:
        """Configure readline for tab completion and line editing."""
        readline.set_completer(self._completer)
        readline.parse_and_bind("tab: complete")
        # macOS uses libedit which needs a different binding
        if "libedit" in readline.__doc__:
            readline.parse_and_bind("bind ^I rl_complete")
        readline.set_completer_delims(" \t")
        # Ensure Ctrl-C raises KeyboardInterrupt inside asyncio
        signal.signal(signal.SIGINT, signal.default_int_handler)

    def _completer(self, text: str, state: int) -> str | None:
        """Tab completion for commands, options, and file paths."""
        line = readline.get_line_buffer()
        parts = line[:readline.get_endidx()].split()

        if not parts or (len(parts) == 1 and not line.endswith(" ")):
            # Completing command name
            matches = [c + " " for c in self._commands() if c.startswith(text)]
        else:
            cmd = parts[0].lower()
            matches = self._complete_args(cmd, text)

        return matches[state] if state < len(matches) else None

    def _complete_args(self, cmd: str, text: str) -> list[str]:
        """Generate completions for command arguments."""
        if cmd == "image":
            # Complete file paths + effect names + "force"
            files = self._complete_path(text)
            effects = [e + " " for e in EFFECTS if e.startswith(text)]
            keywords = [k + " " for k in ("force",) if k.startswith(text)]
            return files + effects + keywords
        elif cmd == "power":
            return [o + " " for o in ("on", "off") if o.startswith(text)]
        elif cmd == "clock":
            opts = [str(i) + " " for i in range(8) if str(i).startswith(text)]
            opts += [o + " " for o in ("12h", "24h", "date") if o.startswith(text)]
            return opts
        elif cmd == "wave":
            gens = ["sine", "pulse", "noise", "rain", "flat", "file", "help"]
            matches = [g + " " for g in gens if g.startswith(text)]
            return matches + self._complete_path(text)
        elif cmd in ("connect", "select"):
            names = [n + " " for n in self._names if n.lower().startswith(text.lower())]
            return names
        else:
            return self._complete_path(text)

    @staticmethod
    def _complete_path(text: str) -> list[str]:
        """Complete file paths."""
        expanded = os.path.expanduser(text)
        matches = glob.glob(expanded + "*")
        result = []
        for m in matches:
            if os.path.isdir(m):
                result.append(m + "/")
            else:
                result.append(m + " ")
        return result

    def _build_prompt(self) -> str:
        if self._active >= 0:
            name = self._names[self._active]
            return f"[{self._active + 1}:{name}] > "
        return "> "

    def _commands(self) -> dict:
        return {
            "connect": self._cmd_connect,
            "disconnect": self._cmd_disconnect,
            "list": self._cmd_list,
            "select": self._cmd_select,
            "scan": self._cmd_scan,
            "status": self._cmd_status,
            "pause": self._cmd_pause,
            "brightness": self._cmd_brightness,
            "speed": self._cmd_speed,
            "image": self._cmd_image,
            "text": self._cmd_text,
            "gif": self._cmd_gif,
            "playlist": self._cmd_playlist,
            "playlist-add": self._cmd_playlist_add,
            "playlist-rm": self._cmd_playlist_rm,
            "wave": self._cmd_wave,
            "wave-style": self._cmd_wave_style,
            "wave-style-raw": self._cmd_wave_style_raw,
            "clock": self._cmd_clock,
            "time": self._cmd_time,
            "power": self._cmd_power,
            "help": self._cmd_help,
            "quit": self._cmd_quit,
            "exit": self._cmd_quit,
        }

    async def _dispatch(self, line: str) -> None:
        parts = shlex.split(line)
        cmd, args = parts[0].lower(), parts[1:]
        handler = self._commands().get(cmd)
        if handler is None:
            raise ShellError(f"Unknown command: {cmd}. Type 'help' for commands.")
        await handler(args)

    # ── Shell commands ───────────────────────────────────────────────

    async def _cmd_connect(self, args: list[str]) -> None:
        """connect <device> — Connect to a device (name, short name, or address)"""
        if not args:
            raise ShellError("Usage: connect <device>  (name, short name, or address)")
        target = args[0]
        display = SurplifeDisplay(target)
        await display.connect()
        await display.init()
        name = display.name or target
        self._displays.append(display)
        self._names.append(name)
        self._active = len(self._displays) - 1
        print(f"Connected to {name} [{self._active + 1}]  {display.status_str}")

    async def _cmd_disconnect(self, args: list[str]) -> None:
        """disconnect [n|name] — Disconnect device (active if omitted)"""
        if args:
            idx = self._resolve_index(args[0])
        else:
            if self._active < 0:
                raise ShellError("No device connected.")
            idx = self._active

        await self._displays[idx].disconnect()
        name = self._names[idx]
        self._displays.pop(idx)
        self._names.pop(idx)
        if self._active >= len(self._displays):
            self._active = len(self._displays) - 1
        elif self._active > idx:
            self._active -= 1
        print(f"Disconnected from {name}")

    async def _cmd_list(self, _args: list[str]) -> None:
        """list — Show connected devices"""
        if not self._displays:
            print("No devices connected.")
            return
        for i, (display, name) in enumerate(zip(self._displays, self._names)):
            marker = "*" if i == self._active else " "
            status = "connected" if display.is_connected else "disconnected"
            print(f"  {marker}[{i + 1}] {name:15s}  {display.address}  ({status})")

    async def _cmd_select(self, args: list[str]) -> None:
        """select <n|name> — Switch active device"""
        if not args:
            raise ShellError("Usage: select <number or name>")
        idx = self._resolve_index(args[0])
        self._active = idx
        print(f"Selected [{idx + 1}] {self._names[idx]}")

    async def _cmd_scan(self, _args: list[str]) -> None:
        """scan — Scan for nearby displays"""
        from .scanner import discover
        devices = await discover(timeout=10.0)
        if not devices:
            print("No Surplife displays found.")
            return
        print(f"Found {len(devices)} display(s):")
        for d in devices:
            connected = any(
                disp.address == d.address for disp in self._displays
            )
            marker = " (connected)" if connected else ""
            print(f"  {d.name:15s}  {d.address}  RSSI={d.rssi}{marker}")

    async def _cmd_status(self, _args: list[str]) -> None:
        """status — Show current device status"""
        d = self._require_active()
        print(d.status_str)

    async def _cmd_pause(self, args: list[str]) -> None:
        """pause <seconds> — Wait (e.g. pause 0.5)"""
        if not args:
            raise ShellError("Usage: pause <seconds>")
        duration = float(args[0])
        await asyncio.sleep(duration)
        print(f"Paused {duration}s")

    async def _cmd_brightness(self, args: list[str]) -> None:
        """brightness <0-100> — Set display brightness"""
        if not args:
            raise ShellError("Usage: brightness <0-100>")
        value = int(args[0])
        await self._require_active().set_brightness(value)

    async def _cmd_speed(self, args: list[str]) -> None:
        """speed <1-100> — Set animation speed (GIF only, see help)"""
        if not args:
            raise ShellError("Usage: speed <1-100>")
        value = int(args[0])
        await self._require_active().set_speed(value)

    async def _cmd_image(self, args: list[str]) -> None:
        """image <file> [effect] [speed] [force] — Upload and show an image"""
        if not args:
            raise ShellError(
                "Usage: image <file> [effect] [speed] [force]\n"
                "Effects: static(1) scroll-r(2) scroll-l(3) flicker(4) "
                "breathe(5) snowflake(6) blend(7) sweep(8) bands(9) wipe(10)"
            )
        path = args[0]
        effect = 1
        speed = 50
        force = False
        for arg in args[1:]:
            if arg == "force":
                force = True
            elif arg in EFFECTS:
                effect = EFFECTS[arg]
            else:
                try:
                    val = int(arg)
                    if 1 <= val <= 10 and effect == 1:
                        effect = val
                    elif 1 <= val <= 100:
                        speed = val
                except ValueError:
                    raise ShellError(f"Unknown option: {arg}")
        c_hash = await self._upload(lambda d: d.show_image_file(
            path, effect=effect, speed=speed, force=force))
        print(f"hash={c_hash.hex()}")

    async def _cmd_text(self, args: list[str]) -> None:
        """text <message> [colors] [speed] [force] — Upload scrolling text"""
        if not args:
            raise ShellError(
                'Usage: text "message" [ff0000,00ff00,...] [speed] [force]\n'
                'Colors are hex RGB, comma-separated or space-separated.\n'
                'Multiple colors = gradient. Default: red.'
            )
        message = args[0]
        colors = []
        speed = 50
        force = False
        from .color import parse_rgb_hex
        for arg in args[1:]:
            if arg == "force":
                force = True
                continue
            # Try as comma-separated color list
            parts = arg.split(",")
            if all(len(p) == 6 for p in parts):
                try:
                    for p in parts:
                        colors.append(parse_rgb_hex(p))
                    continue
                except ValueError:
                    pass
            # Try as speed
            try:
                speed = int(arg)
            except ValueError:
                raise ShellError(f"Unknown option: {arg}")
        c_hash = await self._upload(lambda d: d.show_text(
            message, colors=colors or None, speed=speed, force=force))
        print(f"hash={c_hash.hex()}")

    async def _cmd_gif(self, args: list[str]) -> None:
        """gif <file> [speed] [force] — Upload and play a GIF animation"""
        if not args:
            raise ShellError("Usage: gif <file> [speed] [force]")
        path = args[0]
        speed = 50
        force = False
        for arg in args[1:]:
            if arg == "force":
                force = True
            else:
                try:
                    speed = int(arg)
                except ValueError:
                    raise ShellError(f"Unknown option: {arg}")
        c_hash = await self._upload(lambda d: d.show_gif_file(
            path, speed=speed, force=force))
        print(f"hash={c_hash.hex()}")

    async def _cmd_playlist(self, _args: list[str]) -> None:
        """playlist — Show the device playlist (carousel)"""
        entries = await self._require_active().get_playlist()
        if not entries:
            print("Playlist is empty.")
            return
        for e in entries:
            pos = f"#{e.index}" if e.index else "--"
            print(f"  {pos:3s}  {e.hash.hex()}  {e.duration:3d}s  flag={e.flag:02x}")

    async def _cmd_playlist_add(self, args: list[str]) -> None:
        """playlist-add <hash|last> [seconds] — Add uploaded content to the playlist"""
        if not args:
            raise ShellError("Usage: playlist-add <hash|last> [seconds]")
        d = self._require_active()
        if args[0] == "last":
            c_hash = self._last_hash.get(d)
            if c_hash is None:
                raise ShellError("Nothing uploaded yet on this device.")
        else:
            c_hash = _parse_hash(args[0])
        duration = PLAYLIST_DEFAULT_DURATION
        if len(args) > 1:
            try:
                duration = int(args[1])
            except ValueError:
                raise ShellError(f"Invalid duration: {args[1]}")
            if not 1 <= duration <= 255:
                raise ShellError("Duration must be 1-255 seconds")
        entries = await d.playlist_add(c_hash, duration=duration)
        print(f"Playlist: {len(entries)} entries")

    async def _cmd_playlist_rm(self, args: list[str]) -> None:
        """playlist-rm <hash> — Remove content from the playlist (hash prefix ok)"""
        if not args:
            raise ShellError("Usage: playlist-rm <hash>")
        d = self._require_active()
        prefix = args[0].lower()
        matches = [e for e in await d.get_playlist() if e.hash.hex().startswith(prefix)]
        if not matches:
            raise ShellError(f"No playlist entry starts with {prefix}")
        if len(matches) > 1:
            raise ShellError(f"Ambiguous prefix {prefix}, matches {len(matches)} entries")
        entries = await d.playlist_remove(matches[0].hash)
        print(f"Removed {matches[0].hash.hex()}. Playlist: {len(entries)} entries")

    async def _cmd_wave_style(self, args: list[str]) -> None:
        """wave-style <style> [colors] — Configure waveform (styles: 1,2,3,4,7,8,12,13)"""
        if not args:
            raise ShellError(
                "Usage: wave-style <style> [ff0000,00ff00,...]\n"
                "Styles: 1,2,3,4,7,8,12,13\n"
                "Use wave-style-raw for raw byte control.")
        style = int(args[0])
        colors = None
        if len(args) > 1:
            from .color import parse_rgb_hex
            parts = args[1].split(",")
            try:
                colors = [parse_rgb_hex(p) for p in parts]
            except ValueError:
                raise ShellError(f"Invalid color: {args[1]}")
        await self._require_active().configure_waveform(style=style, colors=colors)

    async def _cmd_wave_style_raw(self, args: list[str]) -> None:
        """wave-style-raw <hex> — Send raw e1 05 bytes (after e1 05 prefix)"""
        if not args:
            raise ShellError(
                "Usage: wave-style-raw <hex bytes after e1 05>\n"
                "Known-good example (style 1 from trace):\n"
                "  wave-style-raw 0064010005640000000000000000000000000000000000a100000004a1006464a1126464a11e6464a13c6464")
        try:
            data = bytes.fromhex(args[0])
        except ValueError:
            raise ShellError("Invalid hex string")
        await self._require_active().configure_waveform_raw(data)

    async def _cmd_wave(self, args: list[str]) -> None:
        """wave <generator> [param] — Stream waveform (Ctrl-C to stop)"""
        generators = {
            "sine": "Scrolling sine wave [freq]",
            "triangle": "Triangle wave",
            "sawtooth": "Sawtooth wave",
            "pulse": "Bouncing pulse [speed]",
            "noise": "Random noise [amplitude]",
            "rain": "Smooth drifting noise [speed]",
            "flat": "Constant level [0-100]",
            "file": "Stream from file bytes <path>",
            "once": "Single ampframe: wave once <generator> [param]",
        }
        if not args or args[0] == "help":
            lines = [
                "Usage: wave <generator> [param]",
                "  Configure style first with wave-style.",
                "Generators:",
            ]
            for name, desc in generators.items():
                lines.append(f"  {name:8s} — {desc}")
            raise ShellError("\n".join(lines))

        gen = args[0]
        param_args = args[1:]
        d = self._require_active()

        from . import waveform

        style_id = 1
        fps = 20
        frame_delay = 1.0 / fps

        once = False
        if gen == "once" and param_args:
            once = True
            gen = param_args[0]
            param_args = param_args[1:]

        if gen == "flat":
            level = int(param_args[0]) if param_args else 50
            await d.send_waveform(waveform.flat(level), style_id=style_id)
            print(f"Flat level={level}")
            return

        if gen == "file":
            if not param_args:
                raise ShellError("Usage: wave file <path>")
            with open(param_args[0], "rb") as f:
                file_data = f.read()
            print(f"Streaming {len(file_data)} bytes ({len(file_data)//96} frames). Ctrl-C to stop.")
            offset = 0
            try:
                while True:
                    frame = waveform.from_bytes(file_data, offset)
                    await d.send_waveform(frame, style_id=style_id)
                    offset = (offset + 96) % max(96, len(file_data))
                    await asyncio.sleep(frame_delay)
            except (KeyboardInterrupt, asyncio.CancelledError):
                print("\nStopped.")
            return

        # Animated generators
        gen_funcs = {
            "sine": lambda: waveform.animate_sine(
                freq=float(param_args[0]) if param_args else 2.0),
            "triangle": lambda: waveform.triangle(
                phase=(time.monotonic() % 1.0),
                amplitude=int(param_args[0]) if param_args else 80),
            "sawtooth": lambda: waveform.sawtooth(
                phase=(time.monotonic() % 1.0),
                amplitude=int(param_args[0]) if param_args else 80),
            "pulse": lambda: waveform.animate_pulse(
                speed=float(param_args[0]) if param_args else 1.0),
            "noise": lambda: waveform.animate_noise(
                amplitude=int(param_args[0]) if param_args else 80),
            "rain": lambda: waveform.animate_rain(
                speed=float(param_args[0]) if param_args else 1.0),
        }
        if gen not in gen_funcs:
            raise ShellError(f"Unknown generator: {gen}")

        if once:
            # Use static generators for single-shot (no time-based phase)
            static_funcs = {
                "sine": lambda: waveform.sine(
                    freq=float(param_args[0]) if param_args else 2.0),
                "pulse": lambda: waveform.pulse(
                    position=float(param_args[0]) if param_args else 0.5),
                "noise": lambda: waveform.noise(
                    amplitude=int(param_args[0]) if param_args else 80),
                "rain": lambda: waveform.perlin_noise(amplitude=80),
                "triangle": lambda: waveform.triangle(
                    amplitude=int(param_args[0]) if param_args else 80),
                "sawtooth": lambda: waveform.sawtooth(
                    amplitude=int(param_args[0]) if param_args else 80),
            }
            func = static_funcs.get(gen, gen_funcs.get(gen))
            if func is None:
                raise ShellError(f"Unknown generator: {gen}")
            frame = func()
            await d.send_waveform(frame, style_id=style_id)
            print(f"Sent single {gen} ampframe")
            return

        print(f"Streaming {gen} at {fps}fps. Ctrl-C to stop.")
        try:
            while True:
                frame = gen_funcs[gen]()
                await d.send_waveform(frame, style_id=style_id)
                await asyncio.sleep(frame_delay)
        except (KeyboardInterrupt, asyncio.CancelledError):
            print("\nStopped.")

    async def _cmd_clock(self, args: list[str]) -> None:
        """clock [style] [12h] [date] — Show clock (style 0-7, options: 12h, date)"""
        style = 0
        hour_24 = True
        show_date = False
        for arg in args:
            if arg == "12h":
                hour_24 = False
            elif arg == "24h":
                hour_24 = True
            elif arg == "date":
                show_date = True
            else:
                try:
                    style = int(arg)
                except ValueError:
                    raise ShellError(f"Unknown option: {arg}")
        await self._require_active().show_clock(style, hour_24, show_date)

    async def _cmd_time(self, args: list[str]) -> None:
        """time [YYYY-MM-DD HH:MM:SS] — Sync device clock (defaults to now)"""
        import datetime
        dt = None
        if args:
            try:
                dt = datetime.datetime.strptime(" ".join(args), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                raise ShellError("Usage: time [YYYY-MM-DD HH:MM:SS]")
        await self._require_active().set_time(dt)

    async def _cmd_power(self, args: list[str]) -> None:
        """power <on|off> — Set display power"""
        if not args or args[0].lower() not in ("on", "off", "true", "false"):
            raise ShellError("Usage: power <on|off>")
        d = self._require_active()
        if args[0].lower() in ("on", "true"):
            await d.power_on()
        else:
            await d.power_off()

    async def _cmd_help(self, _args: list[str]) -> None:
        """help — Show this help"""
        print("Commands:")
        seen = set()
        for name, handler in self._commands().items():
            if handler in seen:
                continue
            seen.add(handler)
            doc = (handler.__doc__ or name).strip()
            print(f"  {doc}")
        print()
        print("Image effects: " + ", ".join(
            f"{name}({eid})" for name, eid in EFFECTS.items()))

    async def _cmd_quit(self, _args: list[str]) -> None:
        """quit / exit — Disconnect all and exit"""
        raise _Quit()

    # ── Helpers ──────────────────────────────────────────────────────

    async def _upload(self, action) -> bytes:
        """Run an upload on the active display and remember its hash."""
        d = self._require_active()
        c_hash = await action(d)
        self._last_hash[d] = c_hash
        return c_hash

    def _resolve_index(self, ref: str) -> int:
        """Resolve a device reference (1-based number or name) to an index."""
        try:
            idx = int(ref) - 1
            if 0 <= idx < len(self._displays):
                return idx
            raise ShellError(f"No device [{ref}]. Use 'list' to see devices.")
        except ValueError:
            pass

        ref_upper = ref.upper()
        for i, name in enumerate(self._names):
            if name.upper() == ref_upper or name.upper().endswith(ref_upper):
                return i
        raise ShellError(f"No device matching '{ref}'. Use 'list' to see devices.")

    async def _disconnect_all(self) -> None:
        for display in self._displays:
            if display.is_connected:
                await display.disconnect()
        self._displays.clear()
        self._names.clear()
        self._active = -1


def _parse_hash(text: str) -> bytes:
    """Parse a 32-hex-digit content hash."""
    try:
        h = bytes.fromhex(text)
    except ValueError:
        raise ShellError(f"Invalid hash: {text}")
    if len(h) != 16:
        raise ShellError(f"Hash must be 16 bytes (32 hex digits), got {len(h)}")
    return h


class ShellError(Exception):
    """User-facing error in the shell."""


class _Quit(Exception):
    """Raised to exit the shell REPL."""


def run_shell(initial_device: str | None = None) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        shell = Shell()
        shell.run_sync(loop, initial_device=initial_device)
        print("Bye!")
    finally:
        loop.close()
