# Third-party software

Input Overlay's own code is MIT licensed (see `LICENSE`). The Windows program is built with, and ships, the components below. Each keeps its own
licence; none of them is changed by this project.

| Component | Version | Licence | Where it is |
|---|---|---|---|
| [SDL3](https://libsdl.org) | 3.4.16 | zlib | `lib/SDL3.dll` (reads controllers) |
| [libusb](https://libusb.info) | 1.0.30 | LGPL-2.1-or-later | `lib/libusb-1.0.dll` (reads the official GameCube adapter and the Switch 2 Pro Controller) |
| [pynput](https://github.com/moses-palmer/pynput) | 1.8.2 | LGPL-3.0 | Python package (global keyboard and mouse hooks) |
| [pystray](https://github.com/moses-palmer/pystray) | 0.19.5 | LGPL-3.0 | Python package (the tray icon) |
| [aiohttp](https://github.com/aio-libs/aiohttp) | 3.14 | Apache-2.0 AND MIT | Python package (the local web server) |
| aiosignal, frozenlist, multidict, propcache, yarl | | Apache-2.0 | Python packages used by aiohttp |
| aiohappyeyeballs | | PSF-2.0 | Python package used by aiohttp |
| attrs, six | | MIT | Python packages |
| idna | | BSD-3-Clause | Python package |
| [Pillow](https://python-pillow.org) | 12.3 | MIT-CMU | Python package (draws the tray icon) |
| [Python](https://python.org) | 3.12 | PSF-2.0 | the interpreter inside the built program |
| [PyInstaller](https://pyinstaller.org) | 6.x | GPL-2.0-or-later with a special exception | builds the program; the exception allows the built program to be distributed under any licence |
| OpenSSL, libffi, the Microsoft Visual C++ runtime | | Apache-2.0, MIT, Microsoft redistributable terms | DLLs that come with Python |

## Fonts

| Font | Where it is | Status |
|---|---|---|
| "IO Pixel" | `overlay/themes/pixel/pixel.ttf` | **Origin and licence unverified.** The font file has no copyright string in its name table. To be confirmed by the author. |

## Checksums of the bundled libraries

The two DLLs in `lib/` are the unmodified official builds. Their SHA-256 checksums are recorded here and in `lib/SHA256SUMS.txt`, and the build
fails if either file does not match, so you can compare them with a copy you download yourself from the projects above:

```
1f98969319302a100931f4385e5918a0bd53ab07773040682d22e7edb54858c0  lib/SDL3.dll         (SDL 3.4.16, Windows x64 release)
7cbf37e76dae9c840c7e8dbf7348ee8897dcc86c8ba45e46ada60b89411569f7  lib/libusb-1.0.dll  (libusb 1.0.30, VS2022\MS64\dll)
```

The Python packages are pinned, with hashes, in `requirements.lock` (the program) and `requirements-dev.lock` (the program plus the build tools),
and builds install them with `--require-hashes`, so every build uses exactly the same packages.

## About the LGPL components

libusb, pynput and pystray are used unmodified. You can replace them: `libusb-1.0.dll` is an ordinary file in the program's `lib` folder (swap in any
build of libusb 1.0), and the Python packages are installed from the public package index when the program is built from this repository, so
building with another version of them (edit `requirements.txt`, regenerate the lock files, and run `build.bat`) gives a program that uses it. Their source code is available from
the project links above.

## Trademarks

Xbox, PlayStation, Nintendo Switch, Switch 2, GameCube, Steam and the Steam Controller are trademarks of their owners. Input Overlay is an independent
project and is not affiliated with or endorsed by them. The controller drawings are original artwork that only depicts those products' layout.
