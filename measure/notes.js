/* Renders margin notes with the page's own display code (web/format.js), for measurement M4.
   stdin: {"sheet": {...fact sheet...}, "items": [{"id": "...", "lang": "en"}]}
   stdout: [{"id", "lang", "locked", "group", "value", "body"}] */
"use strict";
const path = require("path");
const fs = require("fs");
const F = require(path.resolve(process.argv[2]));
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const out = input.items.map((it) => {
  const fact = input.sheet.facts[it.id];
  const locked = F.isLocked(it.id, fact);
  const group = F.noteGroup(it.id);
  let value = "", body = "";
  if (locked) {
    const ids = group.startsWith("group:")
      ? Object.keys(input.sheet.facts).filter((k) => F.noteGroup(k) === group && input.used.includes(k))
      : [it.id];
    const note = F.noteText(group, ids, input.sheet, it.lang, () => null, null);
    value = note.value;
    body = note.body;
  }
  return { id: it.id, lang: it.lang, locked, group, value, body };
});
process.stdout.write(JSON.stringify(out));
