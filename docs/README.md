# Documentation

The manual lives in [`frontend/docs/`](../frontend/docs/), not here.

It is one set of files with one job: ShellMate fetches them at runtime for the
**Docs** button, and they are bundled into the executable so the manual works
on a machine with no internet. Keeping a second copy in this folder for GitHub
to render would guarantee the two drift apart, and the one users actually read
is the one inside the application.

## Editing

Edit the files in `frontend/docs/` directly. Changes appear on the next reload
— there is no build step.

The renderer is `frontend/js/markdown.js`, a deliberately small subset:
headings, paragraphs, lists, tables, fenced code, blockquotes, horizontal
rules, and inline code, bold, italic and links. It is not CommonMark, so check
anything unusual in the app rather than trusting a GitHub preview.

Two things to know:

- Only `http(s)://` and `#` links are rendered. Anything else is left as
  plain text, so a document cannot introduce a `javascript:` URL.
- A `#anchor` link is resolved against the *page* names in the `PAGES` list in
  `frontend/js/docs.js` — `[Device awareness](#device-awareness)` opens that
  page. Add a page there and it appears in the sidebar.

## Publishing to the GitHub wiki

If you want the same content on the wiki, copy it up rather than maintaining
it twice:

```
git clone https://github.com/sjohnston1972/shellmate-portable.wiki.git
cp frontend/docs/*.md shellmate-portable.wiki/
cd shellmate-portable.wiki && git add -A && git commit -m "docs: sync" && git push
```
