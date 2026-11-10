"""Tiny tolerant DOM built on html.parser (no third-party libraries).

    root = dom.parse(html_text)
    for card in root.find_all("div", cls="job-card"):
        title = card.find("h3").text()
        link = card.find("a").attrs.get("href")
"""

import html as _html
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.parent = parent

    def classes(self):
        return (self.attrs.get("class") or "").split()

    def matches(self, tag=None, cls=None, attr=None):
        if tag and self.tag != tag:
            return False
        if cls and cls not in self.classes():
            return False
        if attr:
            name, value = attr
            if name not in self.attrs or (value is not None and self.attrs[name] != value):
                return False
        return True

    def find_all(self, tag=None, cls=None, attr=None):
        out, stack = [], list(reversed(self.children))
        while stack:
            n = stack.pop()
            if isinstance(n, Node):
                if n.matches(tag, cls, attr):
                    out.append(n)
                stack.extend(reversed(n.children))
        return out

    def find(self, tag=None, cls=None, attr=None):
        found = self.find_all(tag, cls, attr)
        return found[0] if found else EMPTY

    def text(self, sep=" "):
        parts = []
        # Explicit stack instead of recursion: broken HTML can nest thousands deep
        stack = [("node", self)]
        while stack:
            kind, item = stack.pop()
            if kind == "str":
                parts.append(item)
                continue
            if kind == "end":
                parts.append("\n")
                continue
            for c in reversed(item.children):
                if isinstance(c, str):
                    stack.append(("str", c))
                else:
                    if c.tag in BLOCK:
                        stack.append(("end", None))   # newline after the block
                    stack.append(("node", c))
                    if c.tag in BLOCK or c.tag == "br":
                        stack.append(("end", None))   # newline before the block / at <br>
        raw = "".join(parts)
        lines = [" ".join(line.split()) for line in raw.split("\n")]
        return "\n".join(l for l in lines if l) if sep == "\n" else " ".join(" ".join(lines).split())

    def __bool__(self):
        return self.tag != "#empty"


EMPTY = Node("#empty")


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v if v is not None else "") for k, v in attrs}, self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, {k: (v or "") for k, v in attrs}, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self.cur = n.parent

    def handle_data(self, data):
        if data:
            self.cur.children.append(data)


def parse(text):
    b = _Builder()
    try:
        b.feed(text)
        b.close()
    except Exception:
        pass  # tolerate broken markup: keep what was parsed
    return b.root


def unescape(s):
    return _html.unescape(s or "")
