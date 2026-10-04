// Keyboard layout + renderer shared by the overlay (index.html) and the settings editor (settings.html).
// Every key is positioned absolutely from the layout grid, so hiding keys keeps the remaining keys where they
// were and the overlay simply crops to whatever is left visible.
const KB = (() => {
  const U = 44, G = 4;   // key size and gap in px

  // token = label:vk:width ("_" label = empty spacer)
  const MAIN = [
    "Esc:27:1 _:0:1 F1:112 F2:113 F3:114 F4:115 _:0:.5 F5:116 F6:117 F7:118 F8:119 _:0:.5 F9:120 F10:121 F11:122 F12:123",
    "`:192 1:49 2:50 3:51 4:52 5:53 6:54 7:55 8:56 9:57 0:48 -:189 =:187 Bksp:8:2",
    "Tab:9:1.5 Q:81 W:87 E:69 R:82 T:84 Y:89 U:85 I:73 O:79 P:80 [:219 ]:221 \\:220:1.5",
    "Caps:20:1.75 A:65 S:83 D:68 F:70 G:71 H:72 J:74 K:75 L:76 ;:186 ':222 Enter:13:2.25",
    "Shift:160:2.25 Z:90 X:88 C:67 V:86 B:66 N:78 M:77 ,:188 .:190 /:191 Shift:161:2.75",
    "Ctrl:162:1.25 Win:91:1.25 Alt:164:1.25 Space:32:6.25 Alt:165:1.25 Win:92:1.25 Menu:93:1.25 Ctrl:163:1.25",
  ];
  const NAV = [
    "PrtSc:44 ScrLk:145 Pause:19",
    "Ins:45 Home:36 PgUp:33",
    "Del:46 End:35 PgDn:34",
    "_:0:3",
    "_:0:1 ↑:38",
    "←:37 ↓:40 →:39",
  ];

  // The numeric keypad. Token: label:vk:width:height (height in rows). The first row is an empty spacer so it lines up with the number row.
  // Numpad Enter has the same virtual-key code as the main Enter (the program tells them apart by Windows' "extended key" flag and reports
  // it as 269), and the digits only count while NumLock is on (with it off Windows reports Home, End, the arrows and so on instead).
  // The Num Lock key is labelled "Num" because "NumLk" does not fit a 1-unit key at the shared label size.
  const NUMPAD = [
    "_:0:4",
    "Num:144 /:111 *:106 -:109",
    "7:103 8:104 9:105 +:107:1:2",
    "4:100 5:101 6:102",
    "1:97 2:98 3:99 Enter:269:1:2",
    "0:96:2 .:110",
  ];

  // `sizes` maps a key's vk code to a width in key units, overriding the default. Keys after a resized key in its row shift to follow.
  function place(rows, x0, sizes) {
    const keys = [];
    rows.forEach((line, y) => {
      let x = x0;
      for (const tok of line.split(' ')) {
        const [label, vk, w = '1', h = '1'] = tok.split(':');
        let width = parseFloat(w);
        if (label !== '_') {
          const o = sizes && sizes[vk];
          if (o > 0) width = o;
          keys.push({ label, vk: +vk, x, y, w: width, h: parseFloat(h) });
        }
        x += width;
      }
    });
    return keys;
  }
  function layout(sizes, numpad) {
    const main = place(MAIN, 0, sizes);
    const mainWidth = Math.max(...main.map(k => k.x + k.w));
    const nav = place(NAV, mainWidth + .35, sizes);
    if (!numpad) return [...main, ...nav];
    return [...main, ...nav, ...place(NUMPAD, Math.max(...nav.map(k => k.x + k.w)) + .35, sizes)];
  }
  const ALL = layout(null, true);                             // every key there is (key lists, groups, default widths)
  const NUMPAD_VKS = layout(null, true).slice(layout(null, false).length).map(k => k.vk);
  const DEFAULT_W = Object.fromEntries(ALL.map(k => [k.vk, k.w]));
  const MIN_W = 0.5, MAX_W = 12, SNAP = 0.25;                 // limits for resizing, and the step it snaps to

  // Quick-select groups for the settings page.
  const range = (a, b) => Array.from({ length: b - a + 1 }, (_, i) => a + i);
  const GROUPS = {
    'Letters': range(65, 90),
    'Digits': range(48, 57),
    'F-row': range(112, 123),
    'Arrows': [37, 38, 39, 40],
    'Numpad': NUMPAD_VKS,
    'Modifiers': [160, 161, 162, 163, 164, 165, 91, 92, 20, 9],
    'WASD': [87, 65, 83, 68],
    'WASD + common': [49, 50, 51, 52, 53, 9, 81, 87, 69, 82, 65, 83, 68, 70, 160, 90, 88, 67, 86, 162, 164, 32],
  };

  // Shift/Ctrl/Alt can also arrive as their generic codes; light both sides.
  const GENERIC = { 16: [160, 161], 17: [162, 163], 18: [164, 165] };

  /**
   * Render into `host`. Returns Map(vk -> [elements]).
   *   visible: Set of vk codes to show, or null for all
   *   editor:  render every key (hidden ones get class "off") and don't crop
   *   sizes:   per-key width overrides, {vk: units}
   *   numpad:  include the numeric keypad
   */
  function render(host, { visible = null, editor = false, sizes = null, numpad = false } = {}) {
    host.textContent = '';
    const els = new Map();
    const all = layout(sizes, numpad);
    const shown = all.filter(k => editor || !visible || visible.has(k.vk));
    if (!shown.length) { host.style.width = host.style.height = '0'; return els; }
    const crop = editor ? all : shown;
    const minX = Math.min(...crop.map(k => k.x)), minY = Math.min(...crop.map(k => k.y));
    const maxX = Math.max(...crop.map(k => k.x + k.w)), maxY = Math.max(...crop.map(k => k.y + k.h));
    host.style.position = 'relative';
    host.style.width = ((maxX - minX) * (U + G) - G) + 'px';
    host.style.height = ((maxY - minY) * (U + G) - G) + 'px';
    for (const k of shown) {
      const el = document.createElement('div');
      el.className = 'key' + (editor && visible && !visible.has(k.vk) ? ' off' : '');
      el.textContent = k.label;
      el.dataset.vk = k.vk;
      el.dataset.len = k.label.length;      // label length and key width (in units) let themes size long labels
      el.dataset.w = k.w;
      Object.assign(el.style, {
        position: 'absolute', left: (k.x - minX) * (U + G) + 'px', top: (k.y - minY) * (U + G) + 'px',
        width: k.w * (U + G) - G + 'px', height: k.h * (U + G) - G + 'px',
      });
      host.appendChild(el);
      if (!els.has(k.vk)) els.set(k.vk, []);
      els.get(k.vk).push(el);
    }
    return els;
  }

  return { ALL, NUMPAD_VKS, GROUPS, GENERIC, render, layout, DEFAULT_W, MIN_W, MAX_W, SNAP, U, G };
})();
