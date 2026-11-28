# Assets and licences

The site's own code is MIT, as `../LICENSE` says. The files under `assets/vendor/ddlc/` come from [ddlc-themes](https://github.com/rokokol/ddlc-themes) and keep their own terms; the two faces are not covered by the MIT licence at all

## Vendored from ddlc-themes

| File | What | Licence |
| --- | --- | --- |
| `ddlc-tokens.css` | the DDLC roles as custom properties, with no selector of an application | MIT, as the repository that generates it |
| `ddlc-ui.css` | the pixel kit: `.bar`, `.card`, `.button`, `.meter`, `dialog` and the rest | MIT |
| `ddlc-report.css` | the prose sheet: the taped quote, the kitty code window and the "Just Monika." pop-up | MIT |
| `ddlc-theme.js` | the three-state theme button | MIT |
| `ddlc-cloud.js` | the colour probe and the ordered dithering the canvases use | MIT |

Every file is a verbatim copy of a commit of that repository, and `.github/vendor.lock` in the parent records the commit and the blob. The colours are Team Salvato's, measured off [ddlc.moe](https://ddlc.moe/) by [ddlc-palette](https://github.com/rokokol/ddlc-palette). This project is unaffiliated with and not endorsed by Team Salvato

## Fonts

| File | Font | Author | Licence |
| --- | --- | --- | --- |
| `DepartureMono-Regular.woff2` | [Departure Mono](https://github.com/rektdeckard/departure-mono) | Helena Zhang | SIL OFL 1.1, in `DepartureMono-LICENSE.txt` |
| `MyaNunito.woff2` | Mya Nunito, a Modified Version of Nunito, Noto Sans and Noto Sans Math | The Nunito Project Authors and the Noto Project Authors | SIL OFL 1.1, in `MyaNunito-LICENSE.txt` |
| `MyaNunito-Italic.woff2` | the same face in italic | The Nunito Project Authors and the Noto Project Authors | SIL OFL 1.1, in `MyaNunito-LICENSE.txt` |

Both licence texts sit beside the fonts and are unmodified. `Doki` is named by the kit as the heading face and is not shipped here: `data-ddlc-heading="plain"` on `<html>` sets the heading in Mya Nunito instead, so a reader without the face sees the same page as everyone else

## The project's own images

`assets/logo.svg`, `assets/icon-github.svg`, `assets/cover.html` and `assets/cover.png` are the site's own work under the repository's MIT licence. `logo.svg` is also the favicon: one mark, one file, and it is a shape rather than a picture: the page masks it and paints it with the accent role, while the file itself carries plum for a light ground and pink for a dark one, both DDLC palette roles, for a favicon or a preview that reads the file alone

`icon-github.svg` draws the GitHub mark from the [Octicons](https://github.com/primer/octicons) set, which GitHub publishes under MIT. The mark itself is a trademark of GitHub, used here to link to the project's own repository

`cover.png` is rendered from `cover.html` by a headless browser, as `README.md` describes, so the two cannot disagree
