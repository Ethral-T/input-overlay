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

## About the LGPL components

libusb, pynput and pystray are used unmodified. You can replace them: `libusb-1.0.dll` is an ordinary file in the program's `lib` folder (swap in any
build of libusb 1.0), and the Python packages are installed from the public package index when the program is built from this repository, so
building with another version of them (see `requirements.txt` and `build.bat`) gives a program that uses it. Their source code is available from
the project links above.

## Trademarks

Xbox, PlayStation, Nintendo Switch, Switch 2, GameCube, Steam and the Steam Controller are trademarks of their owners. Input Overlay is an independent
project and is not affiliated with or endorsed by them. The controller drawings are original artwork that only depicts those products' layout.
