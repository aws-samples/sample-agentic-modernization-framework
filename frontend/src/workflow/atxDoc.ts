// ATX documentation pages are written to be browsed as a linked doc set: each
// opens with its own title, a "[← Back to index](README.md)" link and a
// "## Contents" list of in-page anchors. Inside the portal none of that works
// (the README isn't shown, headings get no anchor ids), so a section rendered
// as-is read "Project Overview / ← Back to index" before any content (APP-CLI).

// The title, plus any immediate repeat of it (technical-debt-report.md carries it twice).
const LEADING_TITLE = /^\s*#{1,3}\s+(.*)\n+(?:#{1,3}\s+\1\n+)*/;
// The nav line: "[← Back to index](../README.md) · [sibling](sibling.md) · …".
const BACK_LINK = /^[ \t]*\[←[^\]]*\]\([^)]*\).*\n?/gm;
// "## Contents" (or "Table of Contents") up to the next heading of any level.
const CONTENTS_BLOCK = /^#{2,3}\s+(?:table of )?contents\s*\n[\s\S]*?(?=^#{1,6}\s)/gim;

/** Strip ATX navigation chrome so a doc reads as a section of the report. */
export function cleanAtxDoc(md: string): string {
  return md
    .replace(LEADING_TITLE, '')
    .replace(BACK_LINK, '')
    .replace(CONTENTS_BLOCK, '')
    .replace(/^\s+/, '');
}
