<div align="center">

# Obsidian Thing site

**The project's business card: one page for what the server does, a tool reference, a short bilingual guide and a cover card for social previews**

[![assets](https://img.shields.io/badge/docs-assets-555)](ASSETS.md)
[![license](https://img.shields.io/badge/code-MIT-3DA639)](../LICENSE)

</div>

## Preview

Serve the folder over HTTP, because a browser does not run the module script from a file URL:

```sh
python3 -m http.server -d site
```

Then open `http://127.0.0.1:8000/`. `?theme=light` or `?theme=dark` pins a variant, and `?lang=ru` pins a language

## How it deploys

`.github/workflows/pages.yml` uploads `site/` as the Pages artifact on every push to `main`, and by hand from the Actions tab. There is no build step: what is in `site/` is what is served. `site/CNAME` holds `obsidian-thing.rokokol.art`, so Pages serves the site on that subdomain

The concurrency group is `pages` with `cancel-in-progress: false`: a newer run waits for the running one and publishes last, so an older deploy cannot overwrite a newer one

## The look

The visual system is the owner's DDLC web kit from [ddlc-themes](https://github.com/rokokol/ddlc-themes), vendored unchanged into `assets/vendor/ddlc/` and pinned in `../.github/vendor.lock`. `ddlc-tokens.css` and `ddlc-ui.css` lay a page out of `.bar`, `.card`, `.button`, `.meter`, `.stats` and `dialog`; `ddlc-report.css` brings the prose sheet, the taped quote and the "Just Monika." pop-up. A page links all three and then `assets/site.css`

`assets/site.css` is the site's own layer and holds no colour of its own: every colour is a `--ddlc-*` role. It adds only what the kit has no class for: the hero and its lattice, the endpoint card, the stickers, the tool groups, the documentation layout and the footer

- The lattice behind the hero is `assets/kit.js` on `<canvas class="lattice">`: a grid of cells lit by three drifting sources and one sweeping band, dithered with the kit's `bayerThresholds`. Every canvas reads its role through the kit's `color()` probe, so a theme change recolours them. Under `prefers-reduced-motion` one still frame is drawn and kept
- The neighbourhood pop-up is a native `<dialog>` with a canvas. It draws what `obsidian.map` answers: the source note and six neighbours on two axes, with the rank coming from the vectors rather than from the drawn position. Coordinates are fixed in the script, and the page contacts nothing
- A number, a source or a count is a `.sticker` or a `.num-sticker`; a principle is a `<blockquote>`, which the report sheet tapes on by itself
- A code block is a `<pre data-lang="...">`, which the report sheet draws as a kitty window, and a one-line command is `.cmd` with its prompt and copy button

## Languages

Every translatable element keeps its English text in the markup and its Russian in a `data-ru` attribute beside it. A translated attribute is `data-ru-aria-label`, `data-ru-alt`, `data-ru-title` or `data-ru-content`, and `<title>` and `<meta name="description">` carry the pair the same way. Code, commands, flags, paths, numbers and product names are the same in both languages

`assets/site.js` collects the English, applies the chosen language on load, and swaps back and forth on the header switch. It writes the choice to `obsidian-thing-lang`, sets `<html lang>`, puts `?lang=` back into the address with `history.replaceState`, and dispatches an `thing:lang` event so `kit.js` can relabel the theme button

The order of preference is `?lang`, then the stored choice, then the browser's language list, then English. With JavaScript off the markup is English, which is why English is the fallback rather than an option. The head script applies a pinned `?theme=light|dark` before the first paint, and sets `<html lang="ru">` for `?lang=ru`

## The cover card

`assets/cover.png` is the 1200 × 630 image that `og:image` and `twitter:image` point at. It is rendered from `assets/cover.html`, which is the card itself at that size, so a change to the words is a change to one page and one command:

```sh
chromium --headless=new --window-size=1200,630 --virtual-time-budget=4000 \
  --screenshot=site/assets/cover.png http://127.0.0.1:8000/assets/cover.html
```

`assets/cover.html` carries `noindex`, and its script draws the same lattice as the hero, with fixed sources so two renders produce the same card

## Where the facts live

The repository is canonical, and these pages do not copy a table from it. Each guide page carries a visible "canonical source" line pointing at the file that owns its subject: `docs/tools.md`, `docs/config.md`, `docs/deployment.md`, `docs/vault-rules.md`, `docs/checker.md` and `docs/semantic-search.md`

`tools.html` is the one page that repeats the tool names, because a visitor wants them on the site. `tests/test_site.py` reads the names out of `src/obsidian_thing/server.py` and fails when the page and the code disagree, so the copy cannot drift in silence

## Editing rules

- Relative paths only, except in `404.html`, which Pages serves at whatever depth the reader asked for and therefore uses root-absolute paths
- No external runtime dependency: the two fonts and every stylesheet and script are vendored here, with no CDN, tracker, analytics or cookie. The only stored values are the theme under `obsidian-thing-theme` and the language under `obsidian-thing-lang`, both in `localStorage`
- Duplicate the header and the footer from an existing page when you add one. The paths differ by one `../` inside `docs/`, and the page under `docs/` marks its own sidebar link with `aria-current="page"`
- Do not edit `assets/vendor/ddlc/`: it is upstream's. Change it in ddlc-themes and let `../vendor-sync.sh update` carry the change here
- Give every table a `<caption>` and every header cell a `scope`
- One `<h1>` per page, one thought per paragraph, no full stop at the end of a paragraph, and no hard wrapping, as in the rest of the repository
- A new tool on `tools.html` carries `data-group` with its switch, and its name and the counts on the filter buttons are checked by the test

## Files

- `index.html` is the landing page: hero, what the agent gets, skills and memory, the four steps of a call, install tabs, documentation links, the plugin
- `features.html`, `tools.html` and `faq.html` are the product pages
- `docs/` holds the short guide: a hub, connecting a client, permissions, and search with its costs
- `404.html` is served at an unknown depth, so it links the shared stylesheet with a root-absolute path
- `assets/site.css` holds the site's layer over the kit, `assets/kit.js` the canvases and the theme button, `assets/site.js` the language switch, the copy buttons, the install tabs and the tool filter
- `assets/vendor/ddlc/` is the vendored kit and its two faces, with their licences beside them
- `assets/logo.svg` is the mark, and it serves as the favicon too, so the mark exists once. On a page the file is a CSS mask and the accent role gives the colour, so the light theme shows a dark mark and the dark theme a light one, and the site's own switch decides rather than the system; the file carries its own light and dark pair for the places that read it alone, where the system does decide
- `assets/cover.html` and `assets/cover.png` are the social preview card and its source

## The tests

`tests/test_site.py` checks what a browser cannot: every internal link resolves to a file, every page carries both languages for its description, every tool name on `tools.html` exists in the server code and every server tool appears on the page, the header and the footer are identical across pages, `CNAME` holds the domain that `og:image` uses, and the cover is a 1200 × 630 PNG
