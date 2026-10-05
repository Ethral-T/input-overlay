# Testing checklist

What has to be tried by hand before the combined work on `testing/combined-prs` becomes a release. Automated tests cover the logic
(`python -m pytest -q tests`, about 80 tests, also run by the build workflow); everything below needs a real keyboard, mouse, controller, OBS or a second
PC, which a test run does not have. Tick items off as they are checked and note the date and what was used (controller model, OBS version, Windows version).

Tip: add `?debug=1` to the overlay URL to see which controller was detected and which inputs it sends.

## 1. Keyboard and mouse

- [ ] **Numpad:** Settings, Keyboard, "Include the numeric keypad": the numpad appears to the right of the arrows, and `?numpad=1` does the same.
- [ ] Numpad digits, `/ * - +`, `.` and Num Lock light with Num Lock **on**. (With it off Windows sends Home, End, the arrows and so on instead.)
- [ ] **Enter and numpad Enter are separate keys:** the main Enter lights only the main Enter, numpad Enter only the numpad one. With the numpad
      hidden, numpad Enter lights nothing. Try typing arrows or Right Ctrl quickly between the two Enters; they must not get mixed up.
- [ ] Hold numpad Enter for 3+ seconds: it stays lit.
- [ ] **Stuck keys:** hold a key, press Win+L, unlock: the key goes dark and its next press shows. Same after holding a key and clicking into a
      game or window that runs as administrator.
- [ ] Mouse buttons, scroll wheel, side buttons (and Mouse Button Invert) and the movement dot work as before, also in a game that locks the cursor.
- [ ] **Labels:** every key label is the same size and on one line, in Classic, Neon and Retro Pixel (Retro Pixel labels are 8px now), including
      PrtSc, ScrLk, Pause, Home, PgUp, PgDn and the numpad's Num. Nothing touches a key's border.

## 2. Controllers

- [ ] **One controller** looks and behaves as before for every skin you own (Steam, Xbox, PlayStation 4/5, Switch Pro, Switch 2 Pro, GameCube).
- [ ] **Quick taps** still flash for a frame and nothing stays lit; unplugging and replugging works.
- [ ] **No freezing:** with a controller connected and **no** GameCube adapter plugged in, the controller must not stutter or freeze every couple of
      seconds (the adapter's USB scan used to block it).
- [ ] **Controllers to show = 2, 3 or 4 with several pads:** player 1 keeps your highlight colour, players 2-4 are red, green, amber (with
      "A colour for each player" on). Each has its own artwork, and a mix of controller types works.
- [ ] **Auto:** plugging controllers in and out adds and removes them on screen, in order. With "Show controller: When connected" and nothing
      plugged in, nothing is drawn; with "Always", one idle controller stays. `?pads=auto` works too.
- [ ] **GameCube adapter in PC mode** (Mayflash): empty ports do not show up as controllers; a pad appears the first time you touch it. The official
      adapter / Wii U mode (needs the WinUSB driver) still works.
- [ ] **Gyro** (tilt and aim box) still works on a real pad, per controller, and the idle skip does not freeze it. The Stream Deck gyro URLs still work.
- [ ] **Switch 2 Pro** over USB still works (Steam must be closed).

## 3. Layout, size and Position (OBS)

- [ ] **Browser Source at 1920x1080 in OBS, Position bottom-left / bottom-middle / bottom-right (and the top and middle ones):** the whole overlay is
      on screen and not cut off. Raise **Size**: it stays on screen (it shrinks to fit if it is bigger than the source) and stays crisp.
      *(This is the fix for the overlay being cut off at the bottom/right in OBS.)*
- [ ] With "Show controller: When connected" and no controller plugged in, the keyboard and mouse sit in the chosen corner (the controller takes no room).
- [ ] **Free layout** (Settings, Preview & layout): drag the keyboard, mouse and a controller; they snap to each other's edges; Reset puts them back in a
      row. OBS shows the same arrangement, and the "OBS source" width and height follow it.
- [ ] Dragging the right edge of a key still resizes it; double-click resets it.

## 4. Settings page

- [ ] **Preview & layout card:** Position, Size, Free layout and the OBS source size sit under the preview. On a window about 1750px wide or more
      the preview is in its own column on the right and stays in view; on a narrower window it hovers across the top; below ~760px tall it does not stick.
      "Keep in view while scrolling" turns the sticking off and is remembered.
- [ ] **Key picker** fits its card at the width where the preview moves to the right, with and without the numpad, and at narrow widths (it scales
      down; clicking and dragging across keys still works).
- [ ] The themes folder row ("Open themes folder") stays inside its card at narrow widths.
- [ ] **The page uses your highlight colour** (live as you change it, per preset or shared) with readable text on buttons for light and dark colours.
- [ ] **Preview shows a controller only when one is connected** (like OBS) when "Show controller" is "When connected"; the tickbox "Show controllers
      even if none is connected" draws it anyway.
- [ ] **The "active" tag follows changes live:** switch preset with a Stream Deck button / hotkey URL (`/api/switch/2`, `/api/next`, `/api/prev`), the
      tray menu, or another browser tab: the tag, the "Make this the active preset" button and the line under it update without reloading.
- [ ] **Changes made elsewhere are not undone:** open Settings, switch gyro from a Stream Deck URL, then change a setting in Settings: the gyro change stays.
- [ ] Rename a preset quickly: no duplicate preset appears. Delete a preset while a save is happening: it does not come back.
- [ ] The slider ranges match what is allowed (Size 0.25-4, sensitivity 0.1-5) and Trackpad tilt has a control.

## 5. Updates (opt-in)

- [ ] Settings, About: "Check for new versions automatically" is **off** by default; nothing contacts the internet.
- [ ] "Check now" reports "up to date" (or a newer version, with a link to the release page). Ticking the box keeps it ticked after a reload.
- [ ] When a newer release exists, a banner shows in Settings and an "Update available" item in the tray menu.

## 6. Two-PC streaming (LAN access)

Needs a second PC (or a phone/tablet browser) on the same network.

- [ ] Starting with `--host 0.0.0.0` and **no** `--allow-lan` shows a message box and exits.
- [ ] `--host 0.0.0.0 --allow-lan <the other PC's address>`: Settings shows the "Another PC (LAN access)" card with a link containing `?token=...`.
- [ ] On the other PC the link loads the overlay in a browser and in an OBS Browser Source, the keys and controller move, and it keeps working
      after OBS is restarted (the cookie) and when the preset is switched.
- [ ] The same link from a **third** device (a different address) is refused (403), and so is the other PC's address without the secret.
- [ ] Ten wrong secrets in a minute lock that address out for five minutes; this PC is unaffected.
- [ ] `--allow-lan 10.0.0.0/8` (or any network wider than a /24) is refused.
- [ ] Over a direct cable or Tailscale (bind `--host` to that address, allow the other PC's) everything works, and the Windows Firewall prompt is answered
      with "Private networks" only.
- [ ] Optional: `--tls-cert` / `--tls-key` with a certificate the other PC trusts serves https and the overlay connects over wss.

## 7. Robustness

- [ ] **Damaged config:** quit the app, edit `%APPDATA%\InputOverlay\config.json` (set one preset's `"controller"` to `[1]`, another's `"keys"` to
      `"abc"`), start: it starts, the other presets survive, `log.txt` says what was skipped. With `[]` as the whole file it keeps `config.bad` and
      starts with the starter presets.
- [ ] **OBS before the app:** start OBS first (blank Browser Source), then the app: the overlay connects by itself and shows the current preset.
- [ ] **A frozen OBS source** (pause the Browser Source's scene or suspend the browser tab) does not make the other overlay pages lag or stop.
- [ ] `--host 0.0.0.0 --allow-lan ...` then starting a second copy opens Settings instead of failing to bind.
- [ ] `log.txt` stays small (it trims itself) and is not full of "connection reset" errors.
- [ ] `?scale=99`, `?sens=abc`, `?controller=foo`, `?accent=orange`, `?accent=f43f5e`, `?pads=9` are all handled (clamped or ignored), and
      `?preset=wasd%20%2B%20mouse` finds "WASD + Mouse".

## 8. Themes and artwork

- [ ] Classic, Retro Pixel and Neon all look right with the keyboard (with and without numpad), the mouse, and every controller skin; Retro Pixel's
      body fill and outline are correct on every controller, including when several are shown.
- [ ] A custom theme in `%APPDATA%\InputOverlay\themes` still loads (Refresh). A theme whose artwork contains a script or an outside link is shown
      without them and does not run anything.

## 9. Build and release (needs GitHub)

- [ ] Actions, Build, Run workflow: the "Run tests" step passes, the build and smoke test pass, and the zip contains `docs/THEMES.md` and `docs/theme-template/`.
- [ ] Tag `v1.1.0` (or later) after updating `version.py`: the workflow refuses a tag that doesn't match, and creates a draft release with the zip and its checksum.
- [ ] Unzip the release on a clean PC (or account): it starts, the tray icon appears, and the overlay works in OBS. Note any SmartScreen / antivirus warning.

## Already covered by automated tests (no need to retest by hand)

Config cleaning and migration, presets (rename, cycle, corrupt file), theme file lookup and traversal, the request guard (Origin, Host, cross-site),
page nonce and CSP, the update check against a stand-in server, Enter / numpad Enter code mapping, the gyro toggles, a stalled client being dropped,
LAN access rules (allowed addresses, secret, lock-out, cookie, `/api/lan` only for this PC, wide networks refused).
