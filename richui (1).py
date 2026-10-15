"""
richui - tiny helper layer for Telegram Rich Messages (Bot API 10.1+) with aiogram.

* Rich(): builder for headings 1-6, paragraphs, quotes, expandable quotes (xquote), footer, tables,
  lists, checklists, collapsible blocks, code blocks. Everything is auto-escaped.
* buttons_html()/embed=True: Bot API 10.3 buttons inside the message body.
* send()/edit(): send as a native rich message; if the server/library does not
  support it (or Telegram rejects it) it degrades to classic HTML automatically.
"""
from __future__ import annotations

import contextlib
import html as _h
import logging
import os
import re

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNotFound

try:  # aiogram >= 3.29
    from aiogram.types import InputRichMessage
except ImportError:  # pragma: no cover
    InputRichMessage = None

log = logging.getLogger("runner.rich")
RICH_ENABLED = InputRichMessage is not None


# --------------------------------------------------------------------------- #
# Escaping + inline helpers
# --------------------------------------------------------------------------- #
class Raw(str):
    """Already-safe markup (output of b(), code(), ...)."""


def esc(x) -> str:
    return x if isinstance(x, Raw) else _h.escape(str(x), quote=False)


def _wrap(tag: str, x) -> Raw:
    return Raw(f"<{tag}>{esc(x)}</{tag}>")


def b(x) -> Raw: return _wrap("b", x)
def i(x) -> Raw: return _wrap("i", x)
def u(x) -> Raw: return _wrap("u", x)
def code(x) -> Raw: return _wrap("code", x)
def mark(x) -> Raw: return _wrap("mark", x)
def spoiler(x) -> Raw: return _wrap("tg-spoiler", x)


def link(text, url: str) -> Raw:
    return Raw(f'<a href="{_h.escape(url, quote=True)}">{esc(text)}</a>')


def mention(user_id: int, name: str) -> Raw:
    return link(name, f"tg://user?id={int(user_id)}")


def _join(parts) -> str:
    return "".join(esc(p) for p in parts)


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
class Rich:
    """Chainable rich-message builder: Rich().h(1, "Title").p("text").build()"""

    def __init__(self) -> None:
        self._b: list[str] = []

    def _add(self, s: str) -> "Rich":
        self._b.append(s)
        return self

    def raw(self, markup: str) -> "Rich":
        return self._add(markup)

    def h(self, level: int, *parts) -> "Rich":
        level = max(1, min(6, int(level)))
        return self._add(f"<h{level}>{_join(parts)}</h{level}>")

    def p(self, *parts) -> "Rich":
        return self._add(f"<p>{_join(parts)}</p>")

    def quote(self, *parts, cite=None) -> "Rich":
        tail = f"<cite>{esc(cite)}</cite>" if cite else ""
        return self._add(f"<blockquote>{_join(parts)}{tail}</blockquote>")

    def xquote(self, *parts, cite=None) -> "Rich":
        """Expandable block quotation (Bot API 10.3): tap to expand, tap again to collapse back."""
        tail = f"<cite>{esc(cite)}</cite>" if cite else ""
        return self._add(f"<blockquote expandable>{_join(parts)}{tail}</blockquote>")

    def aside(self, *parts, cite=None) -> "Rich":
        tail = f"<cite>{esc(cite)}</cite>" if cite else ""
        return self._add(f"<aside>{_join(parts)}{tail}</aside>")

    def pre(self, text, lang: str | None = None) -> "Rich":
        body = _h.escape(str(text), quote=False)
        if lang:
            return self._add(f'<pre><code class="language-{_h.escape(lang)}">{body}</code></pre>')
        return self._add(f"<pre>{body}</pre>")

    def table(self, headers, rows, caption=None, bordered=True, striped=True) -> "Rich":
        attrs = (" bordered" if bordered else "") + (" striped" if striped else "")
        out = [f"<table{attrs}>"]
        if caption:
            out.append(f"<caption>{esc(caption)}</caption>")
        if headers:
            out.append("<tr>" + "".join(f"<th>{esc(c)}</th>" for c in headers[:20]) + "</tr>")
        for row in rows:
            cells = ["—" if (c is None or c == "") else c for c in row[:20]]
            out.append("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in cells) + "</tr>")
        out.append("</table>")
        return self._add("".join(out))

    def ul(self, items) -> "Rich":
        return self._add("<ul>" + "".join(f"<li>{esc(x)}</li>" for x in items) + "</ul>")

    def ol(self, items) -> "Rich":
        return self._add("<ol>" + "".join(f"<li>{esc(x)}</li>" for x in items) + "</ol>")

    def checklist(self, items) -> "Rich":
        """items: iterable of (text, done)."""
        lis = "".join(
            f'<li><input type="checkbox"{" checked" if done else ""}/>{esc(t)}</li>' for t, done in items
        )
        return self._add(f"<ul>{lis}</ul>")

    def details(self, summary, *parts, open: bool = False) -> "Rich":
        return self._add(
            f"<details{' open' if open else ''}><summary>{esc(summary)}</summary><p>{_join(parts)}</p></details>"
        )

    def hr(self) -> "Rich":
        return self._add("<hr/>")

    def footer(self, *parts) -> "Rich":
        return self._add(f"<footer>{_join(parts)}</footer>")

    def build(self) -> str:
        return "".join(self._b)

    __str__ = build


# --------------------------------------------------------------------------- #
# Buttons inside the message body (Bot API 10.3: <tg-button-row> / <tg-button>)
# --------------------------------------------------------------------------- #
def buttons_html(markup) -> str:
    """InlineKeyboardMarkup -> <tg-button-row> blocks (callback + url buttons, max 8 per row)."""
    rows = []
    for row in getattr(markup, "inline_keyboard", None) or []:
        cells = []
        for b in row[:8]:
            style = f' style="{_h.escape(b.style, quote=True)}"' if getattr(b, "style", None) else ""
            text = _h.escape(b.text, quote=False)
            if b.callback_data:
                cells.append(f'<tg-button type="callback_data"{style} data="{_h.escape(b.callback_data, quote=True)}">{text}</tg-button>')
            elif b.url:
                cells.append(f'<tg-button type="url"{style} url="{_h.escape(b.url, quote=True)}">{text}</tg-button>')
        if cells:
            rows.append("<tg-button-row>" + "".join(cells) + "</tg-button-row>")
    return "".join(rows)


# --------------------------------------------------------------------------- #
# Fallback: rich HTML -> classic Telegram HTML (<= 4096 chars)
# --------------------------------------------------------------------------- #
def _strip_tags(s: str) -> str:
    return _h.unescape(re.sub(r"<[^>]+>", "", s))


def _table_to_pre(m: re.Match) -> str:
    body = m.group(0)
    cap = re.search(r"<caption>(.*?)</caption>", body, re.S)
    rows = [
        [_strip_tags(c).strip() for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S)]
        for tr in re.findall(r"<tr>(.*?)</tr>", body, re.S)
    ]
    if not rows:
        return ""
    cols = max(len(r) for r in rows)
    widths = [max((len(r[c]) for r in rows if c < len(r)), default=0) for c in range(cols)]
    lines = [" │ ".join((r[c] if c < len(r) else "").ljust(widths[c]) for c in range(cols)).rstrip() for r in rows]
    if len(lines) > 1:
        lines.insert(1, "─┼─".join("─" * w for w in widths))
    out = "<pre>" + _h.escape("\n".join(lines), quote=False) + "</pre>"
    if cap:
        out = f"<b>{_h.escape(_strip_tags(cap.group(1)), quote=False)}</b>\n" + out
    return "\n" + out + "\n"


_KEEP = r"(?:b|i|u|s|code|pre|a|blockquote|tg-spoiler|tg-emoji)"


def to_basic_html(src: str, limit: int = 4000) -> str:
    s = re.sub(r"<table[^>]*>.*?</table>", _table_to_pre, src, flags=re.S)
    s = re.sub(r"<h[1-6]>(.*?)</h[1-6]>", r"\n<b>\1</b>\n", s, flags=re.S)
    s = re.sub(r"<footer>(.*?)</footer>", r"\n<i>\1</i>", s, flags=re.S)
    s = re.sub(r"<hr\s*/?>", "\n──────────\n", s)
    s = re.sub(
        r"<details[^>]*>\s*<summary>(.*?)</summary>(.*?)</details>",
        r"<blockquote expandable><b>\1</b>\n\2</blockquote>", s, flags=re.S,
    )
    s = s.replace("<aside>", "<blockquote>").replace("</aside>", "</blockquote>")
    s = re.sub(r"<cite>(.*?)</cite>", r"\n— \1", s, flags=re.S)
    s = re.sub(r'<input type="checkbox" checked\s*/?>', "☑ ", s)
    s = re.sub(r'<input type="checkbox"\s*/?>', "☐ ", s)
    s = re.sub(r"<li>(.*?)</li>", r"• \1\n", s, flags=re.S)
    s = re.sub(r"</?(?:ul|ol)>", "", s)
    s = re.sub(r"<p>(.*?)</p>", r"\1\n", s, flags=re.S)
    s = s.replace("<mark>", "<b>").replace("</mark>", "</b>")
    s = re.sub(rf"</?(?!{_KEEP}\b)[a-zA-Z][^>]*>", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    if len(s) > limit:  # cutting HTML mid-tag would break parsing -> degrade to plain text
        s = _h.escape(_strip_tags(src)[: limit - 100], quote=False) + "…"
    return s


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #
def _markup(content) -> str:
    return content.build() if isinstance(content, Rich) else str(content)


def _not_modified(exc: Exception) -> bool:
    return "not modified" in str(exc).lower()


def _disable_if_missing(exc: Exception) -> None:
    global RICH_ENABLED
    if isinstance(exc, TelegramNotFound):
        RICH_ENABLED = False
        log.warning("Server has no rich-message support; falling back to classic HTML.")


async def send(bot, chat_id, content, markup=None, embed: bool = False, **kw):
    """Send a rich message (falls back to HTML). embed=True puts the buttons inside the message body.
    Returns the sent Message."""
    body = _markup(content)
    if RICH_ENABLED:
        rich_body, rich_markup = (body + buttons_html(markup), None) if embed and markup else (body, markup)
        try:
            return await bot.send_rich_message(
                chat_id=chat_id, rich_message=InputRichMessage(html=rich_body), reply_markup=rich_markup, **kw
            )
        except (TelegramBadRequest, TelegramNotFound) as exc:
            log.warning("Rich send rejected (%s); using HTML fallback", exc)
            _disable_if_missing(exc)
        except (AttributeError, TypeError, ValueError) as exc:
            log.warning("Rich send unavailable (%s); using HTML fallback", exc)
    return await bot.send_message(
        chat_id, to_basic_html(body), reply_markup=markup, parse_mode="HTML", **kw
    )


async def edit(bot, message, content, markup=None, embed: bool = False) -> bool:
    """Edit a message in place. embed=True puts the buttons inside the message body.
    Returns False when nothing changed."""
    body = _markup(content)
    chat_id, mid = message.chat.id, message.message_id
    if RICH_ENABLED:
        rich_body, rich_markup = (body + buttons_html(markup), None) if embed and markup else (body, markup)
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=mid, rich_message=InputRichMessage(html=rich_body), reply_markup=rich_markup
            )
            return True
        except TelegramBadRequest as exc:
            if _not_modified(exc):
                return False
            log.debug("Rich edit rejected (%s); trying HTML", exc)
        except TelegramNotFound as exc:
            _disable_if_missing(exc)
        except (AttributeError, TypeError, ValueError):
            pass
    try:
        await bot.edit_message_text(
            text=to_basic_html(body), chat_id=chat_id, message_id=mid, reply_markup=markup, parse_mode="HTML"
        )
        return True
    except TelegramBadRequest as exc:
        if _not_modified(exc):
            return False
        # Message can't be edited in place (other type / too old) -> replace it
        with contextlib.suppress(TelegramAPIError):
            await bot.delete_message(chat_id, mid)
        await send(bot, chat_id, content, markup, embed)
        return True
