# Making a theme for Input Overlay

A theme changes how the keyboard, mouse and controller **look**. It is just a folder of CSS, fonts and images, so if you
can write a little CSS you can make one. Themes can't run code and can't load anything from the internet.

## Quick start

1. Copy `docs/theme-template/` to your themes folder and rename it (this becomes the theme's id):
   `%APPDATA%\InputOverlay\themes\my-theme\` (Settings has an **Open themes folder** button, and so does the tray icon).
2. Edit `theme.json` (name, author, description) and `theme.css`.
3. In Settings, open **Appearance → Theme**, press **Refresh**, and pick your theme. The preview updates as you choose.
4. After editing `theme.css`, press **Refresh** again (it reloads the preview). In OBS, right-click the Browser Source,
   Properties, **Refresh cache of current page**.

To share a theme, zip the folder. Whoever receives it unzips it into their themes folder and presses Refresh.

## Folder contents

```
my-theme/
  theme.json        required: name, author, description, version, css
  theme.css         your styles (the file named by "css" in theme.json)
  *.ttf / *.woff2   optional fonts       (reference them with a relative url, see below)
  *.png / *.svg     optional images
  pad-xbox.svg      optional controller artwork replacements (see "Replacing controller artwork")
```

Only these file types are served from a theme: `.css .json .svg .png .gif .jpg .jpeg .webp .ttf .otf .woff .woff2`.
A theme whose folder name matches a built-in one (for example `pixel`) replaces the built-in.

```json
{ "name": "My Theme", "author": "You", "description": "One line about it.", "version": "1.0", "css": "theme.css" }
```

## What you can style

Your stylesheet loads **after** the app's own, so a rule with the same specificity wins. Pressed state is driven by the app, you
only decide how it looks.

### Variables (set them on `:root`)

| Variable | Meaning |
|---|---|
| `--accent` | the user's highlight colour. Use it so your theme respects their choice |
| `--fill` | the "Fill opacity" slider, 0 to 1. Multiply your backgrounds by it: `rgba(20, 24, 33, var(--fill))` |
| `--key-bg`, `--key-border`, `--key-fg` | default key background / border / text colours |
| `--unit`, `--gap` | key size and spacing (44px, 4px) |

### Keyboard

| Selector | What it is |
|---|---|
| `.key` | every key. `.key.on` is a key that is down |
| `.key[data-len="5"]` | label length (use it to shrink long labels). `data-w` is the key width in key units (1, 1.25, 2.25, 6.25 ...) |
| `.key[data-vk="87"]` | one specific key, by Windows virtual-key code (87 = W) |

### Mouse (`#mouseSvg`)

`.shape` is the body, `.hit` the buttons (`.hit.on` when pressed), `.lbl` the L/R labels, `#motion` the movement dot and trail.

### Controller

* **Layered artwork (every controller)**: `#layerSkin`, which contains `#icon` (the artwork) and `#fx` (the lit-up copies of the
  active parts, and the trackpad fingers). `#padSvg[data-skin="steam"]` / `[data-skin="xbox"]` / `[data-skin="ps4"]` / `[data-skin="ps5"]` / `[data-skin="switch"]` / `[data-skin="switch2"]` / `[data-skin="gamecube"]` (and the other skin names) let a theme
  treat each controller differently. Text the overlay draws on top of the artwork (the Steam back-button labels L4/L5/R4/R5) is
  `#layerSkin .lbl`, so a theme can give it a font or colour.

### Whole overlay

`body[data-theme="<id>"]` is set to your theme's id, `#stage` positions the overlay in the Browser Source, `#root` is the overlay itself.

### Fonts

Put the font file in your theme folder and use a **relative** url:

```css
@font-face { font-family: "My Font"; src: url("my-font.woff2") format("woff2"); }
.key { font-family: "My Font", sans-serif; }
```

### Pixel filters

`filter: url(#pixelate-4)`, `url(#pixelate-6)`, `url(#pixelate-8)` and `url(#pixelate-10)` turn anything into 4, 6, 8 or 10 unit blocks. See the built-in
**Retro Pixel** theme, which also ships a pixel font drawn on a grid (sizes in multiples of 8px stay crisp).

The filters sample a block of N/2 units in the middle of each N-unit block. That has to stay well over one screen pixel at any zoom: a smaller sample
rounds to nothing in the browser and the whole picture disappears (this happened in OBS before the sample was widened).

> Note: setting `filter` in CSS replaces a filter that was set by an attribute. The layered artwork (`#icon`) uses the invert filter
> `url(#inv)` (the artwork is drawn dark-on-light), so combine them: `#icon { filter: url(#inv) url(#pixelate-10); }`.

## Replacing controller artwork

A theme can ship its own artwork. Put a file with one of these names in the theme folder and it is used instead of the built-in one
while the theme is selected:

| File | Replaces |
|---|---|
| `steam-controller.svg`, `xbox-controller.svg`, `ps4-controller.svg`, `ps5-controller.svg`, `switch-controller.svg`, `switch2-controller.svg`, `gamecube-controller.svg` | the layered controllers (see "Layered artwork" below) |

**Layered artwork** is an SVG with the body as the first image and then one named group per part, drawn dark on a light body (the overlay
inverts it). The overlay finds parts by group name, and anything missing is simply skipped:

| Controller | Group names |
|---|---|
| all | `A-Button B-Button X-Button Y-Button`, `D-Pad-Up D-Pad-Down D-Pad-Left D-Pad-Right` (`D-Pad-Center` stays put), `Thumbstick--L- Thumbstick--R-` (they move; the group should be the stick cap), `Bumper--L- Bumper--R-`, `Trigger--L- Trigger--R-` |
| Steam | `Select-Button`, `Menu---Pause-Button`, `Steam-Button`, `Triple-Dot-Button`, `Trackpad--L- Trackpad--R-`, `Back-Button--L4- --L5- --R4- --R5-` |
| Xbox | `Select-Button` (View), `Start-Button` (Menu), `Xbox-Home-Button` |
| PlayStation 4 / 5 | `X-Button` (Cross, the bottom one), `Circle-Button`, `Square-Button`, `Triangle-Button`, `DPad-Up DPad-Down DPad-Left DPad-Right`, `Touch-Pad` (lit by the click; a finger dot follows it), `Share-Button` / `Options-Button` on the PS4 and `Select-Button` (Create) / `Menu-Button` on the PS5, `Home-Button` (PS4) or `PlayStation-Button` (PS5), `Bumper--L- Bumper--R-`, `Trigger--L- Trigger--R-` |
| Switch Pro | `DPad-Up DPad-Down DPad-Left DPad-Right` (not `D-Pad-`), `_--Button1` (Minus) and `_--Button` (Plus), `Home-Button`, `Screenshot-Button` (Capture); the face buttons are named by what is printed on them (A is the right-hand button), `Bumper--L- Bumper--R-`, `Trigger--L- Trigger--R-` (ZL/ZR) |
| Switch 2 Pro | the same as Switch Pro, plus `C-Button`, `GL` and `GR` (the back paddles) |
| GameCube | `Start-Button`, `Z-Button`, `C-Stick` (the right stick; there are no bumpers, back or guide buttons, so `Thumbstick--R-` and `Bumper--` aren't used) |

Triggers fill from the top down as they are pulled, so draw them as the shape that is pulled (a tall bar works well). Groups can be
raster images or vector shapes. The view is cropped to the parts automatically. The group names (and which input each is) are in the
`LAYERED` table near the top of the controller code in `overlay/index.html`.

## Rules and safety

* Themes are CSS, fonts and images only. They cannot run scripts.
* The overlay refuses to load anything from outside the app (`@import`, `url(https://...)`, web fonts), so a theme can't track viewers
  or phone home. Keep everything inside the theme folder.
* Still, only install themes from people you trust.
* Keep file sizes reasonable; the whole folder is read when the theme list is built.
