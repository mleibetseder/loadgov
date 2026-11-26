"""A restricted YAML parser: the subset GitHub and Forgejo workflow files actually use, stdlib only.

Supported: block mappings and sequences by indentation; plain, 'single' and "double" quoted scalars
(double-quote escapes, multi-line folding); `|` and `>` block scalars with chomping and indentation
indicators; comments; true/false/null/~, ints and floats; one-line flow sequences `[a, b]` and flow
mappings `{a: b}`; an optional leading `---`.

Refused with an error that names file:line:col and what is not supported (never silently misparsed):
anchors and aliases (`&` `*`), tags (`!`), merge keys (`<<`), complex keys (`?`), several documents,
directives, tabs as indentation, flow collections over several lines, duplicate keys, a `: ` inside a
plain scalar.

Typing rules: mapping keys are always strings (so `on:` is the key "on", as Actions treats it, never the
YAML 1.1 boolean); as values only true/True/TRUE and false/False/FALSE are booleans (`on`, `yes`, `no`
stay strings); null is null/Null/NULL/~/empty; `3.10` is the float 3.1, exactly as in Actions.
"""

from __future__ import annotations

import math
import re


class YamlError(ValueError):
    """A syntax error or an unsupported construct. `str()` is `file:line:col: message`."""

    def __init__(self, message: str, line: int, col: int, filename: str = "<string>"):
        self.message, self.line, self.col, self.filename = message, line, col, filename
        super().__init__(f"{filename}:{line}:{col}: {message}")


class Located:
    """The parse result with a path -> (line, col) table, so later checks can cite positions."""

    def __init__(self, value, locs: dict):
        self.value, self.locs = value, locs

    def where(self, *path) -> tuple[int, int]:
        p = tuple(path)
        while p and p not in self.locs:
            p = p[:-1]
        return self.locs.get(p, (1, 1))


_INT_RE = re.compile(r"^[-+]?[0-9]+$")
_HEX_RE = re.compile(r"^0x[0-9a-fA-F]+$")
_OCT_RE = re.compile(r"^0o[0-7]+$")
_FLOAT_RE = re.compile(r"^[-+]?(\.[0-9]+|[0-9]+(\.[0-9]*)?)([eE][-+]?[0-9]+)?$")
_ESC = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": " ",
    "P": " ",
}
_HEXN = {"x": 2, "u": 4, "U": 8}
MAX_DEPTH = 64  # nesting of block or flow collections
_ONE_LINE = " (multi-line flow collections are not supported)"
_SEQ_START = ("- ", "-\t")
_BLOCK_HEADER = re.compile(r"([|>])(?:([1-9])([-+]?)|([-+])([1-9]?))?(?:[ \t]+#.*)?[ \t]*")


def resolve_plain(text: str):
    """The typed value of a plain scalar (YAML 1.2 core schema; `on`/`yes`/`no` stay strings)."""
    if text in ("", "~", "null", "Null", "NULL"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if _INT_RE.match(text):
        return int(text)
    if _HEX_RE.match(text):
        return int(text, 16)
    if _OCT_RE.match(text):
        return int(text[2:], 8)
    if _FLOAT_RE.match(text):
        return float(text)
    if text in (".inf", ".Inf", ".INF", "+.inf", "+.Inf", "+.INF"):
        return math.inf
    if text in ("-.inf", "-.Inf", "-.INF"):
        return -math.inf
    if text in (".nan", ".NaN", ".NAN"):
        return math.nan
    return text


class _Line:
    __slots__ = ("indent", "lineno", "text")

    def __init__(self, lineno: int, indent: int, text: str):
        self.lineno, self.indent, self.text = lineno, indent, text

    def is_seq(self) -> bool:
        return self.text == "-" or self.text.startswith(_SEQ_START)


class _Parser:
    def __init__(self, text: str, filename: str):
        self.file = filename
        if text.startswith("﻿"):
            text = text[1:]
        self.raw = re.split(r"\r\n|\n|\r", text)
        self.i = 0
        self.virtual: dict[int, _Line] = {}
        self.locs: dict[tuple, tuple[int, int]] = {}
        self.depth = 0

    # ---- errors and line access -----------------------------------------------------------------
    def err(self, msg: str, lineno: int, col: int) -> YamlError:
        return YamlError(msg, lineno, col, self.file)

    def peek(self) -> _Line | None:
        """The next structural line (blank and comment-only lines skipped)."""
        while self.i < len(self.raw):
            if self.i in self.virtual:
                return self.virtual[self.i]
            r = self.raw[self.i]
            body = r.lstrip(" ")
            if body.strip(" \t") == "" or body.startswith("#"):
                self.i += 1
                continue
            if body[0] == "\t":
                raise self.err(
                    "tabs as indentation are not supported (use spaces)", self.i + 1, len(r) - len(body) + 1
                )
            return _Line(self.i + 1, len(r) - len(body), body)
        return None

    def marker(self, ln: _Line) -> None:
        if ln.indent == 0 and ln.text.rstrip() in ("---", "..."):
            raise self.err("multiple documents (`---` / `...`) are not supported", ln.lineno, 1)

    # ---- document -------------------------------------------------------------------------------
    def document(self):
        ln = self.peek()
        if ln is not None and ln.indent == 0 and (ln.text == "---" or ln.text.startswith(("--- ", "---\t"))):
            rest = ln.text[3:].strip(" \t")
            if rest and not rest.startswith("#"):
                raise self.err("content after the document start `---` is not supported", ln.lineno, 5)
            self.i += 1
            ln = self.peek()
        if ln is None:
            return None
        value = self.node(-1, ())
        tail = self.peek()
        if tail is not None:
            self.marker(tail)
            raise self.err("unexpected content (bad indentation?)", tail.lineno, tail.indent + 1)
        return value

    # ---- block structure ------------------------------------------------------------------------
    def node(self, parent: int, path: tuple):
        self.depth += 1
        try:
            if self.depth > MAX_DEPTH:
                ln = self.peek()
                raise self.err(
                    f"collections nested more than {MAX_DEPTH} levels are not supported",
                    ln.lineno if ln else 1,
                    ln.indent + 1 if ln else 1,
                )
            return self._node(parent, path)
        finally:
            self.depth -= 1

    def _node(self, parent: int, path: tuple):
        ln = self.peek()
        if ln is None or ln.indent <= parent:
            return None
        self.marker(ln)
        if ln.text[0] == "%" and ln.indent == 0:
            raise self.err("directives (`%`) are not supported", ln.lineno, 1)
        if ln.is_seq():
            return self.sequence(ln.indent, path)
        if ln.text == "?" or ln.text.startswith(("? ", "?\t")):
            raise self.err("complex keys (`?`) are not supported", ln.lineno, ln.indent + 1)
        if self.split_key(ln) is not None:
            return self.mapping(ln.indent, path)
        self.locs[path] = (ln.lineno, ln.indent + 1)
        return self.value(ln.text, ln, parent, path, 0)

    def sequence(self, n: int, path: tuple):
        out: list = []
        while True:
            ln = self.peek()
            if ln is None or ln.indent != n or not ln.is_seq():
                break
            self.marker(ln)
            p = (*path, len(out))
            self.locs[p] = (ln.lineno, n + 1)
            rest = ln.text[1:]
            stripped = rest.lstrip(" \t")
            col = n + 1 + (len(rest) - len(stripped))
            if stripped == "" or stripped.startswith("#"):
                self.i += 1
            else:
                self.virtual[self.i] = _Line(ln.lineno, col, stripped)
            out.append(self.node(n, p))
        ln = self.peek()
        if ln is not None and ln.indent > n:
            raise self.err("bad indentation after a sequence entry", ln.lineno, ln.indent + 1)
        return out

    def mapping(self, n: int, path: tuple):
        out: dict = {}
        while True:
            ln = self.peek()
            if ln is None or ln.indent != n or ln.is_seq():
                break
            self.marker(ln)
            if ln.text == "?" or ln.text.startswith(("? ", "?\t")):
                raise self.err("complex keys (`?`) are not supported", ln.lineno, ln.indent + 1)
            k = self.split_key(ln)
            if k is None:
                raise self.err("expected `key: value`", ln.lineno, ln.indent + 1)
            key, rest, rest_off = k
            if key == "<<":
                raise self.err("merge keys (`<<`) are not supported", ln.lineno, ln.indent + 1)
            if key in out:
                raise self.err(f"duplicate key {key!r}", ln.lineno, ln.indent + 1)
            p = (*path, key)
            self.locs[p] = (ln.lineno, ln.indent + 1)
            if rest == "" or rest.startswith("#"):
                self.i += 1
                nxt = self.peek()
                if nxt is not None and nxt.indent == n and nxt.is_seq():
                    out[key] = self.sequence(n, p)
                else:
                    out[key] = self.node(n, p)
            else:
                out[key] = self.value(rest, ln, n, p, rest_off)
        ln = self.peek()
        if ln is not None and ln.indent > n:
            raise self.err("bad indentation after a mapping entry", ln.lineno, ln.indent + 1)
        return out

    # ---- keys -----------------------------------------------------------------------------------
    def split_key(self, ln: _Line):
        """(key, rest, offset of rest in ln.text) when the line is `key: ...`, else None."""
        t = ln.text
        c = t[0]
        if c in "[{":
            return None
        if c in "\"'":
            end = self.quoted_end(t)
            if end is None:
                return None
            s = t[end:].lstrip(" \t")
            if s[:1] == ":" and (len(s) == 1 or s[1] in " \t"):
                out, _, _ = self._scan_quoted(t, 1, c, ln.lineno, ln.indent)
                rest = s[1:].lstrip(" \t")
                return "".join(out), rest, len(t) - len(rest)
            return None
        if c in "&*!" and re.search(r":(\s|$)", t):
            what = {"&": "anchors (`&`)", "*": "aliases (`*`)", "!": "tags (`!`)"}[c]
            raise self.err(f"{what} are not supported", ln.lineno, ln.indent + 1)
        if c in "|>%@`":
            return None
        for j, ch in enumerate(t):
            if ch == "#" and j > 0 and t[j - 1] in " \t":
                return None
            if ch == ":" and (j + 1 == len(t) or t[j + 1] in " \t"):
                key = t[:j].rstrip(" \t")
                if not key:
                    return None
                rest = t[j + 1 :].lstrip(" \t")
                return key, rest, len(t) - len(rest)
        return None

    # ---- values ---------------------------------------------------------------------------------
    def value(self, text: str, ln: _Line, parent: int, path: tuple, off: int):
        """The value that starts with `text`, `off` characters into line `ln`."""
        c = text[0]
        col = ln.indent + off + 1
        if c == "&":
            raise self.err("anchors (`&`) are not supported", ln.lineno, col)
        if c == "*":
            raise self.err("aliases (`*`) are not supported", ln.lineno, col)
        if c == "!":
            raise self.err("tags (`!`) are not supported (quote the value if it is text)", ln.lineno, col)
        if c in "@`%":
            raise self.err(f"a plain scalar cannot start with {c!r} (quote it)", ln.lineno, col)
        if c in "|>":
            return self.block_scalar(text, ln, parent, col)
        if c in "[{":
            self.i += 1
            return self.flow_line(text, ln.lineno, col)
        if c in "\"'":
            return self.quoted_multi(text, ln, parent, col)
        if c == "?" and (len(text) == 1 or text[1] in " \t"):
            raise self.err("complex keys (`?`) are not supported", ln.lineno, col)
        if c == "-" and (len(text) == 1 or text[1] in " \t"):
            raise self.err("a block sequence cannot start on the line of its key", ln.lineno, col)
        return self.plain_multi(text, ln, parent, col)

    # ---- plain scalars --------------------------------------------------------------------------
    @staticmethod
    def cut_comment(text: str) -> str:
        for j in range(len(text)):
            if text[j] == "#" and (j == 0 or text[j - 1] in " \t"):
                return text[:j].rstrip(" \t")
        return text.rstrip(" \t")

    def plain_check(self, s: str, lineno: int, col: int) -> None:
        m = re.search(r":(\s|$)", s)
        if m:
            raise self.err(
                "a plain scalar cannot contain `: ` (mapping values are not allowed here; quote it)",
                lineno,
                col + m.start(),
            )

    def plain_multi(self, text: str, ln: _Line, parent: int, col: int):
        first = self.cut_comment(text)
        self.plain_check(first, ln.lineno, col)
        self.i += 1
        if first != text.rstrip(" \t"):  # a comment ended the scalar
            return resolve_plain(first)
        parts = [first]
        pending = 0
        j = self.i
        while j < len(self.raw):
            r = self.raw[j]
            body = r.lstrip(" ")
            if body.strip(" \t") == "":
                pending += 1
                j += 1
                continue
            indent = len(r) - len(body)
            if body.startswith("#") or indent <= parent:
                break
            if body[0] == "\t":
                raise self.err("tabs as indentation are not supported (use spaces)", j + 1, indent + 1)
            stripped = body.strip(" \t")
            piece = self.cut_comment(stripped)
            self.plain_check(piece, j + 1, indent + 1)
            parts.append("\n" * pending if pending else " ")
            parts.append(piece)
            pending = 0
            j += 1
            self.i = j
            if piece != stripped:
                break
        if len(parts) == 1:
            return resolve_plain(first)
        return "".join(parts)  # a multi-line plain scalar is always a string

    # ---- quoted scalars -------------------------------------------------------------------------
    @staticmethod
    def quoted_end(t: str) -> int | None:
        """Index after the closing quote of the quoted string at the start of t, or None."""
        q = t[0]
        j = 1
        while j < len(t):
            ch = t[j]
            if q == '"' and ch == "\\":
                j += 2
                continue
            if ch == q:
                if q == "'" and t[j + 1 : j + 2] == "'":
                    j += 2
                    continue
                return j + 1
            j += 1
        return None

    def _scan_quoted(self, t: str, j: int, q: str, lineno: int, colbase: int):
        """Scan t from index j (just after the opening quote, or at the line start of a continuation).
        Returns (chars, index after the closing quote or -1 when the line ends inside the string, whether
        the line ended with an escaped line break)."""
        out: list[str] = []
        while j < len(t):
            ch = t[j]
            if q == "'":
                if ch == "'":
                    if t[j + 1 : j + 2] == "'":
                        out.append("'")
                        j += 2
                        continue
                    return out, j + 1, False
                out.append(ch)
                j += 1
                continue
            if ch == '"':
                return out, j + 1, False
            if ch == "\\":
                nxt = t[j + 1 : j + 2]
                col = colbase + j + 1
                if nxt == "":
                    return out, -1, True
                if nxt in _ESC:
                    out.append(_ESC[nxt])
                    j += 2
                elif nxt in _HEXN:
                    n = _HEXN[nxt]
                    digits = t[j + 2 : j + 2 + n]
                    if len(digits) != n or not re.fullmatch(r"[0-9a-fA-F]+", digits):
                        raise self.err(f"bad `\\{nxt}` escape in a double-quoted string", lineno, col)
                    out.append(chr(int(digits, 16)))
                    j += 2 + n
                else:
                    raise self.err(f"unknown escape `\\{nxt}` in a double-quoted string", lineno, col)
                continue
            out.append(ch)
            j += 1
        return out, -1, False

    def quoted_multi(self, text: str, ln: _Line, parent: int, col: int) -> str:
        q = text[0]
        pieces, end, esc = self._scan_quoted(text, 1, q, ln.lineno, col - 1)
        self.i += 1
        if end != -1:
            self._check_tail(text[end:], ln.lineno, col + end)
            return "".join(pieces)
        while True:
            if not esc:  # a folded line break drops the trailing blanks of the line before it
                while pieces and pieces[-1] in (" ", "\t"):
                    pieces.pop()
            blanks = 0
            while self.i < len(self.raw) and self.raw[self.i].strip(" \t") == "":
                blanks += 1
                self.i += 1
            if self.i >= len(self.raw):
                raise self.err("unterminated quoted scalar", ln.lineno, col)
            r = self.raw[self.i]
            body = r.lstrip(" \t")
            lead = len(r) - len(body)
            if parent >= 0 and lead <= parent:
                raise self.err(
                    "a multi-line quoted scalar must be indented more than its parent", self.i + 1, 1
                )
            if blanks:
                pieces.append("\n" * blanks)
            elif not esc:
                pieces.append(" ")
            seg, end, esc = self._scan_quoted(body, 0, q, self.i + 1, lead)
            pieces.extend(seg)
            self.i += 1
            if end != -1:
                self._check_tail(body[end:], self.i, lead + end + 1)
                return "".join(pieces)

    def _check_tail(self, rest: str, lineno: int, col: int) -> None:
        s = rest.strip(" \t")
        if s and not (s.startswith("#") and rest[:1] in " \t"):
            raise self.err("unexpected text after the closing quote", lineno, col)

    # ---- block scalars --------------------------------------------------------------------------
    def block_scalar(self, text: str, ln: _Line, parent: int, col: int) -> str:
        m = _BLOCK_HEADER.fullmatch(text)
        if not m:
            raise self.err(
                "bad block scalar header (expected | or > with an optional chomping/indent indicator)",
                ln.lineno,
                col,
            )
        style = m.group(1)
        ind = m.group(2) or m.group(5)
        chomp = m.group(3) or m.group(4) or ""
        self.i += 1
        base: int | None = None
        if ind:
            base = (parent + int(ind)) if parent >= 0 else int(ind)
        lines: list[str] = []
        j = self.i
        while j < len(self.raw):
            r = self.raw[j]
            if r.strip(" ") == "":
                lines.append(r)
                j += 1
                continue
            indent = len(r) - len(r.lstrip(" "))
            if base is None:
                if indent <= parent:
                    break
                base = indent
            if indent < base:
                break
            lines.append(r)
            j += 1
        self.i = j
        k = len(lines)  # trailing blank lines belong to the chomping, not to the content
        while k > 0 and lines[k - 1].strip(" ") == "":
            k -= 1
        body, trailing = lines[:k], len(lines) - k
        b = base or 0
        stripped = [(x[b:] if len(x) > b else "") for x in body]
        out = "\n".join(stripped) if style == "|" else self._fold(stripped)
        if body:
            out += "\n"
            if chomp == "-":
                out = out[:-1]
            elif chomp == "+":
                out += "\n" * trailing
        else:
            out = "\n" * trailing if chomp == "+" else ""
        return out

    @staticmethod
    def _fold(ls: list[str]) -> str:
        out: list[str] = []
        prev: str | None = None
        blanks = 0
        for x in ls:
            if x.strip(" ") == "":
                blanks += 1
                continue
            if prev is None:
                out.append("\n" * blanks)
            elif x[0] in " \t" or prev[0] in " \t":
                out.append("\n" * (blanks + 1))
            elif blanks:
                out.append("\n" * blanks)
            else:
                out.append(" ")
            out.append(x)
            prev = x
            blanks = 0
        return "".join(out)

    # ---- flow collections (one line) --------------------------------------------------------------
    def flow_line(self, text: str, lineno: int, col: int):
        f = _Flow(self, text, lineno, col)
        value = f.value()
        if not f.at_end():
            raise self.err("unexpected text after the flow collection", lineno, col + f.pos)
        return value


class _Flow:
    def __init__(self, p: _Parser, text: str, lineno: int, col: int):
        self.p, self.t, self.lineno, self.col, self.pos = p, text, lineno, col, 0
        self.depth = 0

    def e(self, msg: str, off: int | None = None) -> YamlError:
        return self.p.err(msg, self.lineno, self.col + (self.pos if off is None else off))

    def skip_ws(self) -> None:
        while self.pos < len(self.t) and self.t[self.pos] in " \t":
            self.pos += 1

    def at_end(self) -> bool:
        self.skip_ws()
        return self.pos >= len(self.t) or (self.t[self.pos] == "#" and self.t[self.pos - 1] in " \t")

    def value(self):
        self.skip_ws()
        if self.pos >= len(self.t):
            raise self.e("a flow collection must be closed on the same line" + _ONE_LINE)
        c = self.t[self.pos]
        if c == "[":
            return self.seq()
        if c == "{":
            return self.map()
        if c in "&*!":
            what = {"&": "anchors (`&`)", "*": "aliases (`*`)", "!": "tags (`!`)"}[c]
            raise self.e(f"{what} are not supported")
        if c in "\"'":
            out, end, _ = self.p._scan_quoted(self.t, self.pos + 1, c, self.lineno, self.col - 1)
            if end == -1:
                raise self.e("unterminated quoted scalar in a flow collection (one line only)")
            self.pos = end
            return "".join(out)
        start = self.pos
        while self.pos < len(self.t):
            ch = self.t[self.pos]
            if ch in ",[]{}":
                break
            if ch == ":" and (self.pos + 1 >= len(self.t) or self.t[self.pos + 1] in " \t,]}"):
                break
            if ch == "#" and self.t[self.pos - 1] in " \t":
                break
            self.pos += 1
        return resolve_plain(self.t[start : self.pos].strip(" \t"))

    def deeper(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise self.e(f"flow collections nested more than {MAX_DEPTH} levels are not supported")

    def seq(self):
        self.deeper()
        try:
            return self._seq()
        finally:
            self.depth -= 1

    def _seq(self):
        self.pos += 1
        out = []
        while True:
            self.skip_ws()
            if self.pos >= len(self.t):
                raise self.e("a flow sequence must be closed with `]` on the same line" + _ONE_LINE)
            if self.t[self.pos] == "]":
                self.pos += 1
                return out
            v = self.value()
            self.skip_ws()
            if self.pos < len(self.t) and self.t[self.pos] == ":":
                raise self.e("single-pair mappings inside a flow sequence are not supported")
            out.append(v)
            if self.pos < len(self.t) and self.t[self.pos] == ",":
                self.pos += 1
            elif self.pos < len(self.t) and self.t[self.pos] != "]":
                raise self.e("expected `,` or `]`")

    def map(self):
        self.deeper()
        try:
            return self._map()
        finally:
            self.depth -= 1

    def _map(self):
        self.pos += 1
        out: dict = {}
        while True:
            self.skip_ws()
            if self.pos >= len(self.t):
                raise self.e("a flow mapping must be closed with `}` on the same line" + _ONE_LINE)
            if self.t[self.pos] == "}":
                self.pos += 1
                return out
            if self.t[self.pos] in "[{":
                raise self.e("flow collections as keys are not supported")
            at = self.pos
            k = self.value()
            if not isinstance(k, str):  # keys are always strings: take the text as written
                k = self.t[at : self.pos].strip(" \t")
            self.skip_ws()
            if self.pos >= len(self.t) or self.t[self.pos] != ":":
                raise self.e("a flow mapping entry needs `key: value`")
            self.pos += 1
            self.skip_ws()
            v = None if self.pos < len(self.t) and self.t[self.pos] in ",}" else self.value()
            if k == "<<":
                raise self.e("merge keys (`<<`) are not supported", at)
            if k in out:
                raise self.e(f"duplicate key {k!r}", at)
            out[k] = v
            self.skip_ws()
            if self.pos < len(self.t) and self.t[self.pos] == ",":
                self.pos += 1
            elif self.pos < len(self.t) and self.t[self.pos] != "}":
                raise self.e("expected `,` or `}`")


def parse_located(text: str, filename: str = "<string>") -> Located:
    p = _Parser(text, filename)
    try:
        value = p.document()
    except RecursionError:  # safety net behind MAX_DEPTH
        raise YamlError("nesting is too deep to read", 1, 1, filename) from None
    return Located(value, p.locs)


def parse(text: str, filename: str = "<string>"):
    """Parse one YAML document of the supported subset; raises YamlError for anything else."""
    return parse_located(text, filename).value
