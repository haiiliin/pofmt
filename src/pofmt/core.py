import argparse
import builtins
import codecs
import contextlib
import difflib
import glob
import locale
import os
import re
import sys
import textwrap
import typing as t
import unicodedata
from pathlib import Path

try:
    import pangu
except ModuleNotFoundError:
    pangu = None

MESSAGE_RE = r'"(.*)"[ ]*$'
_cjk_opening_punct = r"[\uff08\u3008\u300a\u300c\u300e\ufe43\u3014\uffe5\u3010\u201c\u2018]"
_cjk_closing_punct = (
    r"[\uff09\u3009\u300b\u300d\u300f\ufe44\u3015\u2026\u2014\uff5e\ufe4f"
    r"\u3001\u3002\uff0c\uff1f\uff01\uff1a\uff1b\u201d\u2019]"
)


class ParseError(Exception):
    pass


def escape_quotes(text: str) -> str:
    return re.sub(r'(?<!\\)"', '\\"', text)


def is_full_width(text: str) -> bool:
    """See https://stackoverflow.com/a/31666966"""
    return unicodedata.east_asian_width(text) in "WAF"


def support_unicode():
    """Check whether operating system supports main symbols or not."""
    encoding = sys.stdout.encoding
    if encoding is None:
        encoding = locale.getpreferredencoding(False)

    try:
        encoding = codecs.lookup(encoding).name
    except Exception:
        encoding = "utf-8"
    return encoding == "utf-8"


@contextlib.contextmanager
def len_with_cjk_width_factor(factor: float) -> t.Generator[None, None, None]:
    builtin_len = builtins.len

    def new_len(text: str) -> int:
        if not isinstance(text, str):
            return builtin_len(text)
        return int(sum(factor if is_full_width(c) else 1 for c in text))

    builtins.len = new_len
    try:
        yield
    finally:
        builtins.len = builtin_len


class Span(t.NamedTuple):
    start: int
    end: int


class Entry:
    def __init__(self, span: Span, msgid: t.List[str], msgstr: t.List[str]) -> None:
        self.span = span
        self.msgid = msgid
        self.msgstr = msgstr

    @staticmethod
    def process_text(lines: t.List[str]) -> str:
        text = escape_quotes("".join(lines))
        if pangu is not None:
            text = pangu.spacing_text(text)
        return text

    def _format_text(
        self, title: str, lines: t.List[str], width: int, reformat: bool = True
    ) -> t.List[str]:
        if not reformat:
            return [f'{title} "{lines[0]}"'] + [f'"{line}"' for line in lines[1:]]

        text = self.process_text(lines)
        if len(title) + len(text) + 3 <= width:
            # 1 space + 2 quotes = 3
            return [f'{title} "{text}"']

        wrapper = textwrap.TextWrapper(width - 2, drop_whitespace=False)
        wrapper.wordsep_re = re.compile(
            wrapper.wordsep_re.pattern.rstrip(") \n")
            + r")|(?<=%s) (?=\w)|(?=%s)))" % (_cjk_closing_punct, _cjk_opening_punct),
            re.VERBOSE,
        )
        if self.msgid == [""]:
            paras = [f"{para}\\n" for para in text.split("\\n")]
            paras[-1] = paras[-1][:-2]
            return [f'{title} ""'] + [f'"{line}"' for para in paras for line in wrapper.wrap(para)]
        return [f'{title} ""'] + [f'"{line}"' for line in wrapper.wrap(text)]

    def format(
        self, width: int, cjk_width_factor: float = 1.8, no_msgid: bool = False
    ) -> t.List[str]:
        with len_with_cjk_width_factor(cjk_width_factor):
            return self._format_text(
                "msgid", self.msgid, width, reformat=not no_msgid
            ) + self._format_text("msgstr", self.msgstr, width)


class Source:
    def __init__(self, filename: str, lines: t.Optional[t.Sequence[str]] = None) -> None:
        self.filename = filename
        if lines is None:
            lines = list(Path(filename).read_text("utf-8").splitlines())
        self.lines = lines
        self._original = self.lines[:]
        self.lineno = -1
        self._entries: t.List[Entry] = []

    def __iter__(self) -> t.Iterator[str]:
        return self

    def __next__(self) -> str:
        self.lineno += 1
        if self.lineno >= len(self.lines):
            raise StopIteration()
        return self.lines[self.lineno]

    def parse_error(self, message: str, lineno: t.Optional[int] = None) -> str:
        if lineno is None:
            lineno = self.lineno
        raise ParseError(f"line {lineno}: {message}")

    def parse(self) -> None:
        if self.lineno >= 0:
            raise RuntimeError("Can't parse multiple times on one source")
        for line in self:
            if line.startswith("#, ") and line[3:].strip() == "fuzzy":
                # Don't modify the fuzzy entries
                self._parse_entry()
            if not line.strip() or line.startswith("#"):
                continue
            elif line.startswith("msgid"):
                self.lineno -= 1
                self._entries.append(self._parse_entry())
            else:
                self.parse_error("Unexpected token")

    def _parse_entry(self) -> Entry:
        msgid, msgstr = [], []
        temp = []
        start_line = self.lineno

        for line in self:
            if not line.strip():
                break
            if line.startswith("#"):
                continue
            if line.startswith("msgid"):
                if msgid:
                    self.lineno -= 1
                    break
                match = re.match(MESSAGE_RE, line[6:])
                if not match:
                    self.parse_error('Expect `msgid "..."`')
                msgid.append(match.group(1))
                start_line = self.lineno
                temp = msgid
            elif line.startswith("msgstr"):
                if msgstr:
                    self.lineno -= 1
                    break
                match = re.match(MESSAGE_RE, line[7:])
                if not match:
                    self.parse_error('Expect `msgstr: "..."`')
                msgstr.append(match.group(1))
                temp = msgstr
            else:
                match = re.match(MESSAGE_RE, line)
                if not match:
                    self.parse_error('Expect `"..."`')
                temp.append(match.group(1))

        if not msgstr:
            self.parse_error("Missing msgstr")
        return Entry(Span(start_line, self.lineno), msgid, msgstr)

    def fix(
        self,
        line_length: int,
        cjk_width: float = 1.8,
        no_msgid: bool = False,
        show: bool = False,
    ) -> bool:
        self.parse()
        for entry in reversed(self._entries):
            self.lines[entry.span.start : entry.span.end] = entry.format(
                line_length, cjk_width, no_msgid
            )
        return self.diff(show)

    def diff(self, show: bool = False) -> bool:
        has_diff = False
        show_title = False
        for line in difflib.unified_diff(
            self._original, self.lines, "Original", "Current", lineterm=""
        ):
            if show:
                if not show_title:
                    print(f"Need update: {self.filename}")
                    show_title = True
                print(line)
            has_diff = True
        return has_diff

    def write(self, path: t.Union[str, Path]) -> None:
        Path(path).write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def cli(argv: t.Optional[t.Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Format PO files for consistency")
    parser.add_argument(
        "--line-length", type=int, default=76, help="The max length of msgid and msgstr"
    )
    parser.add_argument("-c", "--check", action="store_true", help="Check only, don't modify files")
    parser.add_argument(
        "--cjk-width",
        type=float,
        default=1.8,
        help="The width factor of a CJK character, default: 1.8",
    )
    parser.add_argument("--no-msgid", action="store_true", help="Don't format msgid")
    parser.add_argument(
        "filename",
        nargs="*",
        default=[os.getcwd()],
        help="Filenames to format, default to all po files under "
        "the current directory(recursively)",
    )
    args = parser.parse_args(argv)

    identical, changed, errors = 0, 0, 0
    if support_unicode():
        ERROR, SUCCESS = "❌", "✨"
    else:
        ERROR, SUCCESS = ":(", ":)"

    for filename in args.filename:
        if os.path.isdir(filename):
            filename = os.path.join(filename, "**/*.po")
        for path in glob.glob(filename, recursive=True):
            source = Source(path)
            try:
                if source.fix(
                    args.line_length,
                    cjk_width=args.cjk_width,
                    no_msgid=args.no_msgid,
                    show=args.check,
                ):
                    if not args.check:
                        source.write(path)
                        print(f"{SUCCESS} {path} is updated")
                    changed += 1
                else:
                    identical += 1
            except ParseError as e:
                errors += 1
                print(f"{ERROR} {path} Parse error: {e}")
                continue

    print(
        f"\nChecked {identical + changed + errors} file(s), "
        f"{errors} error file(s) and {changed} file(s) changed."
    )
    if changed or errors:
        return 1
    return 0
