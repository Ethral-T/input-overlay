# Input Overlay
n![Input Overlay](docs/input-overlay-promo.png)

Stream overlay showing your keyboard, mouse (buttons, scroll, movement) and Steam Controller
(sticks, buttons, triggers, both trackpads with pressure, back grips).

## Using the program

1. Run `dist\InputOverlay\InputOverlay.exe` (see *Building* below). A tray icon appears; the first launch opens the settings page.
2. In **Settings** (tray icon click, or `http://127.0.0.1:8765/settings`) pick or create a **preset**:
   - click keys to hide/show them (click and drag to paint several), or use the group chips (Letters, WASD, ...)
   - show/hide the mouse and controller, choose the highlight colour and size
3. Add `http://127.0.0.1:8765/` in OBS as a **Browser Source**. It shows the **active preset**, so you never edit the URL again.
   Edits you make in Settings, and preset switches, update running sources instantly (no reload).

Switching the active preset:
- Settings page: "Make this the active preset"
- Tray menu: *Active preset*
- Any HTTP GET (Stream Deck, scripts, `curl`):

  | URL | Effect |
  |---|---|
  | `/api/switch/<name or number>` | activate a preset by name (case-insensitive) or 1-based position, e.g. `/api/switch/2` |
  | `/api/next`, `/api/prev` | cycle through presets (wraps around) |
  | `/api/switch` | just report the active preset |
  | `/api/gyro/tilt/toggle`, `/api/gyro/aim/toggle` | flip the gyro tilt picture / aim dot on or off for **every preset** (it sets Gyro to *all presets*, saved); use `on` or `off` instead of `toggle` to set it |
  | `/api/gyro/on`, `/api/gyro/off`, `/api/gyro/toggle` | both gyro displays together |
  | `/api/gyro` | just report the gyro state: `{"applies_to": "all presets", "gyro": "both", "tilt": true, "aim": true}` |

  The preset ones return `{"active": "<name>"}`. In Stream Deck, use an action that sends a GET in the background (the built-in
  **Website** action with "Access in background" ticked, or an API plugin such as BarRaider's API Ninja). The Settings page
  lists the exact URLs with Copy buttons. These requests are rejected if a browser marks them cross-site, so a web page can't flip your preset.

To pin a source to one preset regardless of switching (e.g. a controller-only source on a second scene), use
`http://127.0.0.1:8765/?preset=<name>`.

Tray menu: open settings, switch preset, copy the OBS URL, start with Windows, quit.

## Resizing keys

In the Settings **preview**, drag the **right edge** of any key to make it wider or narrower (it snaps to quarter-key steps, from 0.5 to 12
units). The keys after it in that row shift to follow, and the overlay re-flows live, so the mouse beside the keyboard moves with it.
**Double-click** a key's edge to put that key back to its default width, or press **Reset key sizes** (under the key picker) to clear them all.
Sizes are saved per preset, and only the Settings preview lets you drag: the page OBS shows has no handles. The starter *WASD + Mouse*
preset comes with the spacebar at 2.5 units so it ends under the V key instead of trailing off to the right.

## Positioning it in OBS

Settings shows the **Browser Source size** to type into OBS (width x height): the size for the selected preset, and the largest
across all presets (use that one for a source that follows the active preset). Then move and resize the source with OBS's handles as usual.

- **Position in source** (3x3 picker, per preset; **centre** by default) pins the overlay to a corner/edge/centre *inside* the source, so switching presets
  doesn't shift it. With a tight source the red box hugs the overlay; with a big one (say 1920x1080) you can pin it to a corner.
- To change the overlay's size, use the **Size** slider (it re-renders crisply) instead of stretching the source in OBS, which blurs it.
- The preview is drawn at the source size you enter, with a dashed line at the source's edge.
- URL equivalents: `?align=br` (tl tc tr ml mc mr bl bc br; default mc) and `?scale=1.5`.

Presets are stored in `%APPDATA%\InputOverlay\config.json` (log: `log.txt` next to it).

## Building

```
build.bat            builds dist\InputOverlay\InputOverlay.exe (folder: starts fast, fewer antivirus false positives)
build.bat onefile    builds a single dist\InputOverlay.exe (slower to start)
```

Requires Python 3.12, `lib\SDL3.dll` (official SDL3 release from github.com/libsdl-org/SDL; the Windows x64 zip) and `lib\libusb-1.0.dll`
(libusb 1.0.30, `VS2022\MS64\dll` from the Windows .7z at github.com/libusb/libusb/releases; it is used to read the official GameCube adapter and the Switch 2 Pro Controller, and is already in `lib\`).
Because the program installs global keyboard/mouse hooks, some antivirus tools flag PyInstaller builds; the folder
build is less likely to be flagged. Distribute the whole `dist\InputOverlay` folder.

For development, `python server.py` runs the server in a console (no tray). Server options: `--port 8765`, `--host 127.0.0.1`,
`--pad N` (controller index when several are connected).

## URL options (override the preset)

| Param | Values |
|---|---|
| `preset` | preset name (default: the first preset) |
| `kb` | `full`, `compact`, `off` |
| `mouse` | `1`, `0` |
| `pad` | `auto` (only while connected), `1`/`on`, `0`/`off` |
| `accent` | colour, e.g. `f43f5e` or `orange` |
| `scale`, `opacity`, `fill`, `sens`, `tpt` | size, overall opacity (0.1-1), fill opacity of keys/mouse/controller backgrounds (0-1), mouse-movement sensitivity, trackpad click pressure (0-1) |
| `tprot` | tilt in degrees of the trackpad finger-tracking frame (default 9) |
| `gyro`, `gsens` | gyro display: `off`, `tilt`, `aim` or `both`, and its sensitivity (see *Gyro* below) |
| `static` | `1` doesn't connect to real input (for screenshots and demos) |
| `theme` | theme id: `default`, `pixel`, `neon`, or the folder name of one you added |
| `controller` | artwork: `auto`, `steam`, `xbox`, `ps4`, `ps5`, `switch`, `switch2`, `gamecube` |
| `debug` | `1` shows the detected controller type/name, button presses and live trackpad pressure in the corner |

## Steam Controller

Read through SDL3, so it works even when Steam isn't presenting a virtual Xbox pad; XInput is the fallback for other
controllers. Trackpads have no click switch: a "click" is pressure past a threshold (`tpt`). The artwork is
`overlay/img/steam-controller.svg` (one image per part, in named groups: keep the ids if you edit it).

## Themes

A theme restyles the keyboard, mouse and controller. Built in: **Classic** (the original look), **Retro Pixel** (8-bit: square bevelled keys,
a pixel font, pixelated mouse and controller) and **Neon** (glowing outlines in your highlight colour).

Pick one in Settings, Appearance, **Theme**. Like opacity, it carries an **all presets / this preset** tag: by default one theme applies
to every preset, click the tag to give a preset its own.

**Adding your own:** each theme is a folder of CSS, fonts and images. Drop it into your themes folder
(`%APPDATA%\InputOverlay	hemes\`, opened by **Open themes folder** in Settings or the tray menu), press **Refresh** next to the theme
picker, and it appears (marked *custom*). Delete the folder to remove it. To make one, copy `docs/theme-template/` and read
`docs/THEMES.md`: it lists everything a theme can style, including replacing the controller artwork.

Themes are CSS and assets only. They can't run code, and the overlay blocks them from loading anything from the internet, but only
install themes from people you trust. URL: `?theme=pixel`.

## Controller styles

**Controller style** (Settings, Controller section; per preset) chooses the artwork: Steam Controller, Xbox, PlayStation 4, PlayStation 5,
Nintendo Switch, Switch 2 Pro or GameCube. **Auto-detect** (the default) picks one from the connected controller's type and name
(a controller whose name contains "Steam" gets the Steam skin; unknown or generic pads get Xbox).
URL: `?controller=xbox` (auto steam xbox ps4 ps5 switch switch2 gamecube).

Every skin (Steam Controller, Xbox, PlayStation 4 and 5, Switch Pro, Switch 2 Pro, GameCube) is layered artwork (`overlay/img/steam-controller.svg`, `xbox-controller.svg`, `ps4-controller.svg`, `ps5-controller.svg`, `switch-controller.svg`, `switch2-controller.svg`, `gamecube-controller.svg`): one named group per
part, drawn dark and inverted by the overlay, so a new controller is a matter of drawing the parts and naming the groups. Triggers
fill from the top down as they are pulled. The original artwork files live in `assets/controllers/`; the copies the overlay uses are in `overlay/img/`
(the d-pads in those copies were evened out from the originals, and the dev scripts for that are kept outside this repository).
PlayStation 4 and 5 are chosen by SDL's controller type (PS3 pads get the PS4 art). The face buttons are matched by position, so `X-Button` in the
art is Cross, the bottom button. The touchpad lights when clicked in, and your finger on it is drawn as a dot that follows the pad's own shape.
The GameCube art follows SDL's button positions for the GameCube adapter (Z arrives as the right shoulder, the C-stick as the right stick) and may need adjusting after a
real test: use `?debug=1` to see which input each button sends, then change its entry in the `gamecube` row of the `LAYERED` table in
`overlay/index.html`.

### Gyro

Controllers with motion sensors (PlayStation 4 and 5, Switch Pro, Steam Controller, where SDL reports them) can show their gyroscope. Choose it in
Settings, Controller, **Gyro** (off by default; it has the usual *all presets / this preset* tag, and the Stream Deck URLs set it to all presets), or with `?gyro=tilt|aim|both`:

- **Tilt the picture**: the controller artwork leans the way you turn the real one, then settles back to level when you stop.
- **Aim dot**: a box beside the controller with a dot that follows the turn and leaves a fading trail, then drifts back to the middle.

A gyroscope measures how fast the controller turns, not where it points, so both displays are "motion" views that recentre by themselves.
**Gyro sensitivity** (`gsens`) scales how far the picture leans and how fast the dot moves. `?debug=1` shows the live gyro rates, or says the
controller doesn't report a gyroscope. The Switch 2 Pro, the GameCube adapters and Xbox pads don't report gyro to this program yet.

### GameCube controllers

Two kinds of adapter work, and both give you the GameCube artwork automatically:

- **Mayflash and other adapters in "PC" mode** show up as an ordinary gamepad, so SDL reads them. Nothing to install.
- **The official Nintendo adapter** (Wii U / Switch, "WUP-028"), or a Mayflash switched to **Wii U mode**, can't be opened by the SDL build we ship
  on Windows, so the program reads it itself through libusb (`gcadapter.py`, `lib\libusb-1.0.dll`). For that to work Windows needs a
  **WinUSB driver on the adapter**: run Zadig, pick "WUP-028" (Options, List All Devices), choose WinUSB and Install (Dolphin's own adapter
  setup installs the same driver). Close Dolphin while using the overlay, since only one program can hold the adapter. If you want the adapter back
  as a normal device later, uninstall that driver in Device Manager.
  The first controller found on the adapter is shown (`--pad N` picks the Nth if several are plugged in).

**Only one program can use a Wii U-mode adapter at a time.** While the overlay is running it holds the adapter, so a game can't read it, and
the other way round. Steam knows this: since its client update of 21 January 2026 it no longer opens Wii U-mode GameCube adapters by default
("opening them is exclusive with other applications"); a game played through Steam Input (Rivals of Aether 2, say) only sees the adapter if
you launch Steam with `-enable-libusb-gamecube`, and then Steam and the overlay will fight over it. So for **playing and streaming at the
same time, use an adapter in PC mode** (a Mayflash switched to PC): the overlay and the game can both read that one. The libusb path is
mainly for testing and for setups where the overlay is the only program using the adapter.

`?debug=1` shows which controller was detected and which inputs it sends. The Z button arrives as the right shoulder and the C-stick as the right stick.

### Switch 2 Pro Controller

The SDL build we ship doesn't know the Switch 2 Pro Controller, so the program reads it itself over **USB** (`switch2.py`; Bluetooth isn't supported).
Plug it in with a cable: there is nothing to install, because Windows already binds the WinUSB driver to the controller's command interface.
It is detected by name and gets its own **Switch 2 Pro** artwork, which has the C button and the two back paddles GL and GR as well as everything
the regular Switch Pro art has (sticks calibrated from the controller's own memory, Home, Capture). ZL and ZR are on/off buttons.

**Only one program can hold the controller's command interface at a time, and Steam takes it.** If Steam is running, the overlay can't start the
controller: quit Steam (tray icon, Exit) first, or let Steam pass the controller to the overlay as a virtual Xbox pad instead (give the controller
a Gamepad layout in Steam; the overlay then shows the Xbox artwork unless you pick Nintendo Switch in Settings).

## Settings shared across presets

Mouse Button Invert, Highlight colour, Fill opacity, Overall opacity and Theme each carry a tag in Settings: **all presets** (highlighted) or **this preset**.
Click the tag to switch that one setting. While it says *all presets* there is one value for every preset; switching back to
*this preset* returns each preset's own stored value. Mouse Button Invert and Theme start as *all presets*, the opacities and the highlight
colour start as *this preset*.

## Updating

After you launch a new build, any open overlay (including OBS Browser Sources) reloads itself, because OBS otherwise keeps
showing the old copy of the page. If an OBS source ever looks out of date, right-click it, Properties, **Refresh cache of current page**.

## Mouse side buttons

Mouse 4 (Back) is drawn as the lower side button and Mouse 5 (Forward) as the upper one. If your mouse reports them the other
way round, tick **Mouse Button Invert** in Settings. By default it applies to every preset; click its tag to make it per-preset.

## Security

The server listens on `127.0.0.1` only, and refuses requests from other websites (Origin/Host checks), because any
page could otherwise connect to the local socket and read your keystrokes. Don't run it with `--host 0.0.0.0` unless you mean to.
The keyboard hook is global: the overlay shows keys typed in any window, including passwords. Hide the source on sensitive screens.

## Antivirus and "unrecognised app" warnings

Windows SmartScreen, browsers and some antivirus scanners (including the aggregate results on VirusTotal) may flag the download or
warn that it is "unrecognised". This is a **false positive**, and it is common for this kind of tool. The reasons:

- **A global keyboard hook.** To show your keys in any window, the program uses a system-wide keyboard and mouse hook (via `pynput`
  and Windows Raw Input). That is exactly how keyloggers work, so heuristic scanners treat the behaviour as suspicious. Input Overlay
  only sends what it sees to `127.0.0.1` (your own PC) and never to the internet, and the source is open so you can check; see [Security](#security).
- **Not code-signed.** The executable has no paid code-signing certificate, so Windows has no publisher to trust and SmartScreen shows
  "Windows protected your PC" until enough people have downloaded the file to build up a reputation. Each new build starts from zero.
- **Built with PyInstaller.** The `.exe` is a Python program packed with PyInstaller, which is also how a lot of real malware is
  packaged. Scanners often flag the packer's bootloader itself, so even a hello-world PyInstaller app can trigger detections.
- **Raw USB access and bundled DLLs.** The GameCube adapter and Switch 2 Pro support talk to the USB device directly through
  `libusb-1.0.dll`, and gamepads are read through `SDL3.dll`. Low-level device access plus unsigned DLLs is another pattern scanners score as risky.
- **A local web server and a tray icon.** The program runs a small web server on your machine and sits in the system tray, which are
  behaviours that malware also uses.
- **Few downloads.** Scanners lean on reputation, and a new, small project has none yet.

**What you can do:** build it yourself from this repository (see [Building](#building)), read the code, or compare the file's
SHA-256 against the one published with each release. If your antivirus quarantines it, you can add an exclusion for the install
folder. Only do that for a copy you downloaded from this repository's Releases page or built yourself.

## Notes

- Games running as administrator may block input hooks from a non-elevated process; run the program elevated if needed.
- Mouse movement uses Windows Raw Input, so it works in games that lock the cursor.
- The numpad is not displayed yet.

## Licence

Input Overlay is free software under the [MIT licence](LICENSE). The program bundles other open-source components (SDL3, libusb and several
Python packages) that keep their own licences: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Xbox, PlayStation, Nintendo, Steam and the other
product names are trademarks of their owners; this project is independent and not affiliated with them.
