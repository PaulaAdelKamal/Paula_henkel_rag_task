"""Quick inventory of the source PDF (pages, fonts, images, running headers, cross-references).

Usage: uv run python scripts/pdf_inventory.py data/source/iPhone_User_Guide_1.pdf
"""

import collections
import hashlib
import re
import sys

import pymupdf as fitz

path = sys.argv[1]
doc = fitz.open(path)
print("pages", doc.page_count, "encrypted", doc.is_encrypted, "toc_entries", len(doc.get_toc()))
print("metadata", {k: v for k, v in doc.metadata.items() if v})
print("page0 size", doc[0].rect, "embedded_files", doc.embfile_count(), "is_form", doc.is_form_pdf)

fonts = collections.Counter()
for p in doc:
    for f in p.get_fonts(full=True):
        fonts[(f[3], f[2], f[1])] += 1
print("fonts (basefont, type, ext):")
for k, v in fonts.most_common(15):
    print("  ", k, v)

img_objs = 0
pages_with_img = 0
xref_pages = collections.defaultdict(set)
hashes = collections.defaultdict(set)
smasks = set()
sizes = []
max_per_page = 0
for p in doc:
    imgs = p.get_images(full=True)
    if imgs:
        pages_with_img += 1
    max_per_page = max(max_per_page, len(imgs))
    for im in imgs:
        xref, smask, w, h = im[0], im[1], im[2], im[3]
        xref_pages[xref].add(p.number + 1)
        if smask:
            smasks.add(smask)
        sizes.append((w, h))
        hashes[hashlib.sha256(doc.xref_stream_raw(xref) or b"").hexdigest()].add(xref)
print(
    "unique image xrefs",
    len(xref_pages),
    "smasks",
    len(smasks),
    "pages_with_images",
    pages_with_img,
    "max_per_page",
    max_per_page,
    "unique content hashes",
    len(hashes),
)
ws = [w for w, _ in sizes]
hs = [h for _, h in sizes]
print(
    "avg w x h",
    round(sum(ws) / len(ws)),
    "x",
    round(sum(hs) / len(hs)),
    "min",
    min(sizes, key=lambda s: s[0] * s[1]),
)
reused = {x: sorted(ps) for x, ps in xref_pages.items() if len(ps) > 1}
print("xrefs reused across pages", len(reused), "e.g.", list(reused.items())[:3])


def norm(text: str) -> str:
    # The text layer uses thin / narrow no-break spaces and tabs between tokens.
    return re.sub(r"[\s\u2009\u202f\u00a0]+", " ", text).strip()


xref_re = re.compile(r"See .{1,80}? on page \d+")
n_xref = 0
running_re = re.compile(r"^(Chapter (\d+)|Appendix ([A-D])) (.+?) (\d+) ")
footer_pages = 0
mismatch = []
no_text = []
for p in doc:
    t = norm(p.get_text())
    if len(t) < 20:
        no_text.append(p.number + 1)
    n_xref += len(xref_re.findall(t))
    m = running_re.match(t)
    if m:
        footer_pages += 1
        if int(m.group(5)) != p.number + 1:
            mismatch.append((p.number + 1, m.group(0)))
print("xref 'See ... on page N' count", n_xref)
print(
    "pages with running header/footer",
    footer_pages,
    "printed!=pdf index",
    mismatch[:10],
    "pages w/o text",
    no_text,
)
for i in (1, 7, 21, 161):
    print(
        f"--- page {i + 1} first/last lines:",
        doc[i].get_text().strip().splitlines()[:3],
        "...",
        doc[i].get_text().strip().splitlines()[-2:],
    )
