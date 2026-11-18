"""Minimal Excel (.xlsx) writer, standard library only: several sheets, a bold frozen header row,
filters on every column, right-to-left sheets (Arabic), numbers kept as numbers."""

import io
import re
import zipfile
from xml.sax.saxutils import escape

_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
MAX_CELL = 32000          # Excel's limit is 32,767 characters per cell


def _col(n):
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref, v, style=0):
    st = f' s="{style}"' if style else ""
    if isinstance(v, bool):
        v = "نعم" if v else ""
    if isinstance(v, (int, float)) and v == v:
        return f'<c r="{ref}"{st}><v>{v}</v></c>'
    text = _BAD.sub("", "" if v is None else str(v))[:MAX_CELL]
    if text.startswith(("=", "+", "-", "@")):
        text = "'" + text   # never a formula (CSV/Excel injection)
    return f'<c r="{ref}" t="inlineStr"{st}><is><t xml:space="preserve">{escape(text)}</t></is></c>'


def _sheet(head, rows):
    widths = [min(60, max([len(str(h))] + [len(str(r[i])) for r in rows[:300] if i < len(r)]) + 2)
              for i, h in enumerate(head)]
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths))
    out = ['<row r="1">' + "".join(_cell(f"{_col(i)}1", h, 1) for i, h in enumerate(head)) + "</row>"]
    for n, r in enumerate(rows, 2):
        out.append(f'<row r="{n}">' + "".join(_cell(f"{_col(i)}{n}", v) for i, v in enumerate(r)) + "</row>")
    last = f"{_col(max(0, len(head) - 1))}{max(1, len(rows) + 1)}"
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetViews><sheetView rightToLeft="1" workbookViewId="0">'
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            f'<cols>{cols}</cols><sheetData>{"".join(out)}</sheetData>'
            f'<autoFilter ref="A1:{last}"/></worksheet>')


def _safe_name(name, used):
    name = re.sub(r"[\[\]:*?/\\]", " ", name)[:31] or "Sheet"
    base, i = name, 2
    while name in used:
        name = f"{base[:28]} {i}"
        i += 1
    used.add(name)
    return name


def workbook(sheets):
    """sheets: [(name, header list, rows list of lists)] -> .xlsx bytes."""
    used = set()
    sheets = [(_safe_name(n, used), h, r) for n, h, r in sheets] or [("Sheet", [""], [])]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                   + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                             for i in range(1, len(sheets) + 1)) + "</Types>")
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                   '</Relationships>')
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{escape(n, {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
                             for i, (n, _, _) in enumerate(sheets, 1))
                   + "</sheets>"
                   + "</workbook>")
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                             for i in range(1, len(sheets) + 1))
                   + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   "</Relationships>")
        z.writestr("xl/styles.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
                   '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
                   '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
                   '<fill><patternFill patternType="solid"><fgColor rgb="FF232862"/></patternFill></fill></fills>'
                   '<borders count="1"><border/></borders>'
                   '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
                   '<cellXfs count="2"><xf/><xf fontId="1" fillId="2" applyFont="1" applyFill="1"/></cellXfs>'
                   '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
                   '</styleSheet>')
        for i, (_, head, rows) in enumerate(sheets, 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet(head, rows))
    return buf.getvalue()
