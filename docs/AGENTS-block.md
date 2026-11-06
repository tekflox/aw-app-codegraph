Before a grep/Read loop to find where something is defined, how a flow
works, or what breaks if you change a symbol, reach for
`codegraph_explore` first — one call takes a natural-language question (or a
bag of symbol/file names) and returns the **verbatim current source** of the
symbols that matter, grouped by file and line-numbered, plus the call path
among them and who else calls them.

That source is re-read from disk on the call, so it is as fresh as a `Read`
— treat it as one: don't re-open a file it already showed you.

Through the gateway the tools are prefixed `aw__codegraph__…` (e.g.
`aw__codegraph__codegraph_explore`). See the **`aw-codegraph`** skill for
the full tool reference (narrower single-purpose tools, index freshness,
what is and isn't indexed).
