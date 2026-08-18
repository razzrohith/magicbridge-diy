#!/usr/bin/env python3
"""Structural check for the web UI's HTML.

    python3 tests/test_html_structure.py [file ...]      # defaults to src/web/index.html

WHY THIS EXISTS. A single stray </div> in src/web/index.html closed
<aside id="sidebar"> early. Nothing errored: the page still loaded, the JS still
ran, `node --check` on the extracted script still passed, and the div OPEN/CLOSE
counts still looked plausible at a glance. The only symptom was that the right
hand rail (nav#railnav) rendered at the bottom-left of the window instead of down
the right edge, because the browser's error recovery re-parented it.

So syntax checking the JavaScript is NOT enough to catch a broken layout, and
counting tags is not enough either - the counts can balance while the NESTING is
wrong. This walks the tree and asserts the real parent/child relationships.

RELATED, and NOT covered here: a temporal-dead-zone error once killed the whole
inline script (an IIFE ran at load and called a function that dereferenced a
`const` declared 2000 lines later, so everything below it never defined - no
WebSocket, no status, no settings panel). That is a RUNTIME fault: it is valid
syntax, so `node --check` passes, and it is not a markup problem, so this test
passes too. A static heuristic for it was tried and produced false positives on
correct code, so it was dropped rather than shipped. The only reliable check is
to LOAD THE PAGE and confirm the script ran to completion, e.g. that the
last-defined functions actually exist:

    ['jigKind','jigSchedSave','refreshJigglerStatus','connect','ping']
        .filter(n => typeof window[n] !== 'function')      // must be []

Do that in a browser after any edit that moves or adds top-level init code.
"""
import re
import sys
from html.parser import HTMLParser

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
        'link', 'meta', 'param', 'source', 'track', 'wbr'}

# id -> the ancestor chain it MUST have. These are the landmarks that decide the
# whole page layout; if any of them re-parents, the UI is visibly wrong.
EXPECTED_PARENTS = {
    'railnav': 'main',
    'sidebar': 'main',
    'view':    'main',
}


class Tree(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parent_of = {}
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        d = dict(attrs)
        ident = d.get('id')
        if ident:
            # nearest ancestor that has an id, else the tag name
            par = next((i for _t, i in reversed(self.stack) if i), None)
            self.parent_of[ident] = par or (self.stack[-1][0] if self.stack else None)
        self.stack.append((tag, ident))

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append("stray </%s> at line %d" % (tag, self.getpos()[0]))
            return
        if self.stack[-1][0] != tag:
            self.errors.append(
                "mismatched </%s> at line %d - innermost open element is <%s%s>"
                % (tag, self.getpos()[0], self.stack[-1][0],
                   (' id=' + self.stack[-1][1]) if self.stack[-1][1] else ''))
            for k in range(len(self.stack) - 1, -1, -1):   # unwind to keep going
                if self.stack[k][0] == tag:
                    del self.stack[k:]
                    return
            return
        self.stack.pop()


def check(path):
    src = open(path, encoding='utf-8').read()
    # Script and style bodies are not markup; a '<' inside a JS string would
    # otherwise look like a tag.
    src = re.sub(r'<script.*?</script>', '', src, flags=re.S)
    src = re.sub(r'<style.*?</style>', '', src, flags=re.S)

    t = Tree()
    t.feed(src)
    fails = []

    for e in t.errors:
        fails.append("nesting: " + e)
    if t.stack:
        fails.append("unclosed at EOF: %s" % ([tag for tag, _ in t.stack],))
    for ident, want in EXPECTED_PARENTS.items():
        got = t.parent_of.get(ident, '** MISSING **')
        if got != want:
            fails.append("#%s should sit inside #%s but is inside %r" % (ident, want, got))

    print("%s" % path)
    if fails:
        for f in fails:
            print("  FAIL  %s" % f)
    else:
        print("  PASS  tree well formed; %d ids; layout landmarks correctly parented"
              % len(t.parent_of))
    return fails


def main():
    targets = sys.argv[1:] or ["src/web/index.html"]
    bad = 0
    for p in targets:
        bad += len(check(p))
    print("\nFAILURES: %d" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
